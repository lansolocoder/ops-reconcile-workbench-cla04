"""Manual disposition of batch findings and decision review.

The ``decide`` subcommand attaches a human decision (``confirm``,
``ignore`` or ``fix`` with a non-empty reason) to one finding of a batch
stored by ``audit-orders --db/--batch``.  The ``review-decisions``
subcommand replays the decisions of an older batch against a newer batch
and explains which decisions still hold.  Only the Python standard
library is used.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterator
from typing import BinaryIO, TextIO

from .diff_audits import finding_identity
from .orders_audit import (
    AuditError,
    _canonical_json,
    emit_report,
    fetch_batch,
    serialize,
)

ACTIONS = ("confirm", "ignore", "fix")

_CREATE_DECISIONS_SQL = """
CREATE TABLE IF NOT EXISTS decisions (
    batch_id TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    finding_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, identity_json)
)
"""


class DecisionConflictError(Exception):
    """A decision with the same identity is stored with other content (exit 3).

    The stored decision is left untouched.
    """

    def __init__(self, batch_id: str, identity: list):
        self.batch_id = batch_id
        self.identity = identity
        ident = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        super().__init__(
            f"a decision for identity {ident} in batch {batch_id!r} is already "
            f"stored with a different action or reason"
        )


@contextlib.contextmanager
def _decision_transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """Hold one immediate SQLite transaction, creating the decisions table.

    Storage errors are reported as :class:`AuditError` (exit 2); anything
    raised inside the block rolls the transaction back, so a failed
    ``decide`` never changes a stored decision.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.isolation_level = None  # explicit BEGIN/COMMIT below
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_CREATE_DECISIONS_SQL)
    except sqlite3.Error as exc:
        raise AuditError(f"cannot open database: {exc}", filename=db_path)
    try:
        try:
            yield conn
        except sqlite3.Error as exc:
            raise AuditError(f"database error: {exc}", filename=db_path)
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise AuditError(f"cannot commit decision: {exc}", filename=db_path)
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def run_decide(
    db_path: str,
    batch_id: str,
    identity_text: str,
    action: str,
    reason: str,
) -> int:
    """Persist a human decision on one batch finding; return exit code.

    ``identity_text`` must be the JSON identity used by ``diff-audits`` and
    must match a finding of ``batch_id``.  ``reason`` is trimmed and must
    stay non-empty.  Repeating the exact same decision is idempotent
    (exit 0); the same identity with a different action or reason raises
    :class:`DecisionConflictError` (exit 3).  A missing batch/database, a
    bad parameter or an identity that matches no finding raises
    :class:`AuditError` (exit 2); the database is never changed.
    """
    if action not in ACTIONS:
        raise AuditError(
            f"ACTION must be one of {', '.join(ACTIONS)}, got {action!r}",
            filename="ACTION",
        )
    reason = reason.strip()
    if not reason:
        raise AuditError(
            "REASON must not be empty after trimming", filename="REASON"
        )
    try:
        identity = json.loads(identity_text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"ID is not valid JSON: {exc}", filename="ID")

    batch = fetch_batch(db_path, batch_id)
    if batch is None:
        raise AuditError(f"batch {batch_id!r} not found", filename=db_path)
    findings = json.loads(batch[2])

    matches = [f for f in findings if finding_identity(f) == identity]
    if not matches:
        ident = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        raise AuditError(
            f"identity {ident} does not match any finding of batch {batch_id!r}",
            filename="ID",
        )
    finding = matches[0]

    identity_json = _canonical_json(identity)
    finding_json = _canonical_json(finding)
    with _decision_transaction(db_path) as conn:
        existing = conn.execute(
            "SELECT action, reason, finding_json FROM decisions "
            "WHERE batch_id = ? AND identity_json = ?",
            (batch_id, identity_json),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (action, reason, finding_json):
                raise DecisionConflictError(batch_id, identity)
            # Identical decision: idempotent, the database is not rewritten.
        else:
            conn.execute(
                "INSERT INTO decisions "
                "(batch_id, identity_json, action, reason, finding_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch_id, identity_json, action, reason, finding_json),
            )
    return 0


def _fetch_decisions(
    db_path: str, batch_id: str
) -> list[tuple[str, str, str, str]]:
    """Return stored decisions of one batch as ``(identity, action, reason, finding)``.

    A database without a decisions table simply has no decisions yet.
    """
    if not os.path.isfile(db_path):
        raise AuditError("database does not exist", filename=db_path)
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT identity_json, action, reason, finding_json "
                "FROM decisions WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise AuditError(f"cannot read database: {exc}", filename=db_path)
    except sqlite3.Error as exc:
        raise AuditError(f"cannot read database: {exc}", filename=db_path)


def build_review(
    old_findings: list[list],
    old_decisions: list[tuple[str, str, str, str]],
    new_findings: list[list],
) -> list[list]:
    """Replay OLD decisions against NEW findings; return ordered JSONL items.

    Each identity contributes a block of one or two rows:

    - an unchanged finding with an OLD decision gives one ``kept`` row;
    - a changed one gives ``invalid``/``changed`` followed by ``pending``;
    - a disappeared one gives ``invalid``/``resolved`` with a null tail;
    - a NEW finding without a kept decision gives one ``pending`` row.

    Blocks are ordered ascending by identity, element by element; within
    one block ``invalid`` always precedes ``pending``.  The ``summary``
    item is appended by the caller.
    """
    new_by_id = {tuple(finding_identity(f)): f for f in new_findings}

    blocks: dict[tuple, list[list]] = {}
    kept_ids: set[tuple] = set()
    for identity_json, action, reason_text, finding_json in old_decisions:
        ident_tuple = tuple(json.loads(identity_json))
        ident = list(ident_tuple)
        decision = [action, reason_text]
        old_finding = json.loads(finding_json)
        new_finding = new_by_id.get(ident_tuple)
        if new_finding is None:
            blocks[ident_tuple] = [
                ["invalid", ident, "resolved", decision, old_finding, None]
            ]
        elif new_finding == old_finding:
            blocks[ident_tuple] = [["kept", ident, decision, new_finding]]
            kept_ids.add(ident_tuple)
        else:
            blocks[ident_tuple] = [
                ["invalid", ident, "changed", decision, old_finding, new_finding],
                ["pending", ident, new_finding],
            ]

    # NEW findings that no kept decision covers: changed identities already
    # carry their pending row above, so only genuinely new ones remain.
    for ident_tuple, new_finding in new_by_id.items():
        if ident_tuple in kept_ids or ident_tuple in blocks:
            continue
        blocks[ident_tuple] = [["pending", list(ident_tuple), new_finding]]

    items: list[list] = []
    # Identities with the same type tag have homogeneous tail types, so the
    # plain element-wise tuple order is total here.
    for ident_tuple in sorted(blocks):
        items.extend(blocks[ident_tuple])
    return items


def run_review(
    db_path: str,
    old_id: str,
    new_id: str,
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Review the decisions of OLD against the findings of NEW; exit code 0.

    A missing database or batch raises :class:`AuditError` (exit 2) before
    anything is written, so no partial results are produced.
    """
    old = fetch_batch(db_path, old_id)
    if old is None:
        raise AuditError(f"batch {old_id!r} not found", filename=db_path)
    new = fetch_batch(db_path, new_id)
    if new is None:
        raise AuditError(f"batch {new_id!r} not found", filename=db_path)

    old_hash, _, old_findings_json = old
    new_hash, _, new_findings_json = new
    items = build_review(
        json.loads(old_findings_json),
        _fetch_decisions(db_path, old_id),
        json.loads(new_findings_json),
    )

    kept = sum(1 for item in items if item[0] == "kept")
    invalid = sum(1 for item in items if item[0] == "invalid")
    pending = sum(1 for item in items if item[0] == "pending")
    items.append(["summary", kept, invalid, pending, old_hash, new_hash])

    emit_report(serialize(items), output_path, stdout)
    return 0
