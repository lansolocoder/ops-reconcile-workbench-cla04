"""Implementation of the ``decide`` and ``review-decisions`` subcommands.

A decision is a manual disposition of one finding stored by
``audit-orders --db DB --batch ID``.  It records the action
(``confirm``/``ignore``/``fix``), a non-empty reason and a snapshot of the
complete finding the decision was made against, keyed by the finding
identity used by :mod:`ops_workbench.diff_audits`.

``review-decisions`` replays the decisions of an OLD batch against a NEW
batch and explains which decisions still apply.  Only the Python standard
library is used.
"""

from __future__ import annotations

import contextlib
import json
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
    """A decision with the same identity is already stored differently.

    Exit status 3; the stored decision is left untouched.
    """

    def __init__(self, batch_id: str, identity: list):
        self.batch_id = batch_id
        self.identity = identity
        super().__init__(
            f"a different decision for identity {json.dumps(identity)} "
            f"is already stored for batch {batch_id!r}"
        )


@contextlib.contextmanager
def _decision_transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """Hold one immediate SQLite transaction, creating the decisions table."""
    try:
        conn = sqlite3.connect(db_path)
        conn.isolation_level = None
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


def _parse_identity(raw: str) -> list:
    """Parse the ID argument (a ``diff-audits`` identity JSON array)."""
    try:
        identity = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"ID is not valid JSON: {exc}", filename="ID")
    if not isinstance(identity, list):
        raise AuditError("ID must be a JSON array", filename="ID")
    return identity


def _index_findings(findings: list[list]) -> dict[tuple, list]:
    return {tuple(finding_identity(f)): f for f in findings}


def run_decide(
    db_path: str,
    batch_id: str,
    identity_text: str,
    action: str,
    reason: str,
) -> int:
    """Store one manual decision; return the exit code.

    The ID must be a ``diff-audits`` finding identity that hits a finding of
    the stored batch; ACTION must be ``confirm``/``ignore``/``fix`` and the
    REASON must be non-empty after trimming.  The decision and the complete
    finding are written in a single transaction.

    Repeating the identical decision is idempotent (0); a stored decision
    with the same identity but different action/reason/finding raises
    :class:`DecisionConflictError` (3) and changes nothing.  A missing
    database/batch/identity or a bad argument raises :class:`AuditError`
    (2).
    """
    identity = _parse_identity(identity_text)

    if action not in ACTIONS:
        raise AuditError(
            f"ACTION must be one of {', '.join(ACTIONS)}", filename="ACTION"
        )
    reason = reason.strip()
    if not reason:
        raise AuditError("REASON must not be empty after trimming",
                         filename="REASON")

    batch = fetch_batch(db_path, batch_id)
    if batch is None:
        raise AuditError(f"batch {batch_id!r} not found", filename=db_path)
    findings = json.loads(batch[2])
    finding = _index_findings(findings).get(tuple(identity))
    if finding is None:
        raise AuditError(
            f"identity {json.dumps(identity)} does not match a finding of "
            f"batch {batch_id!r}",
            filename="ID",
        )

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
            # Identical decision: idempotent, the row is not rewritten.
        else:
            conn.execute(
                "INSERT INTO decisions "
                "(batch_id, identity_json, action, reason, finding_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch_id, identity_json, action, reason, finding_json),
            )
    return 0


def _fetch_decisions(db_path: str, batch_id: str) -> list[tuple[str, str, str, str]]:
    """Return stored ``(identity_json, action, reason, finding_json)`` rows."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
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
    return rows


def review_decisions(
    new_findings: list[list],
    decision_rows: list[tuple[str, str, str, str]],
) -> tuple[list[list], int, int, int]:
    """Replay OLD ``decision_rows`` against the NEW finding set.

    Returns ``(items, kept_count, invalid_count, pending_count)`` with items
    ordered ascending by identity; for one identity an ``invalid`` item
    precedes its ``pending`` item.  The summary item is appended by the
    caller.  A decision keeps while its identity still hits the same
    complete finding in NEW; otherwise the finding changed or resolved and
    the identity is additionally reported as pending.
    """
    new_by_id = _index_findings(new_findings)

    items: list[list] = []
    kept = invalid = 0
    kept_identities: set[tuple] = set()

    # Sort the decisions too, so the final ordering pass stays stable.
    for identity_json, action, reason, finding_json in sorted(decision_rows):
        identity = json.loads(identity_json)
        ident_tuple = tuple(identity)
        decision = [action, reason]
        old_finding = json.loads(finding_json)
        new_finding = new_by_id.get(ident_tuple)
        if new_finding is not None and new_finding == old_finding:
            items.append(["kept", identity, decision, new_finding])
            kept += 1
            kept_identities.add(ident_tuple)
            continue
        if new_finding is None:
            items.append(
                ["invalid", identity, "resolved", decision, old_finding, None]
            )
        else:
            items.append(
                ["invalid", identity, "changed", decision,
                 old_finding, new_finding]
            )
        invalid += 1

    for ident_tuple, new_finding in new_by_id.items():
        if ident_tuple not in kept_identities:
            # rank 1 keeps pending after a same-identity invalid item.
            items.append(["pending", list(ident_tuple), new_finding])

    def sort_key(item: list) -> tuple:
        rank = 1 if item[0] == "pending" else 0
        return tuple(item[1]), rank

    items.sort(key=sort_key)
    pending = len(new_by_id) - len(kept_identities)
    return items, kept, invalid, pending


def run_review(
    db_path: str,
    old_id: str,
    new_id: str,
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Review the OLD batch's decisions against the NEW batch; exit code.

    All database reads finish before anything is written, so a read or write
    failure raises :class:`AuditError` (2) without partial results; an
    existing output file is preserved by the atomic write.
    """
    old = fetch_batch(db_path, old_id)
    if old is None:
        raise AuditError(f"batch {old_id!r} not found", filename=db_path)
    new = fetch_batch(db_path, new_id)
    if new is None:
        raise AuditError(f"batch {new_id!r} not found", filename=db_path)

    old_hash, _, _ = old
    new_hash, _, new_findings_json = new
    rows = _fetch_decisions(db_path, old_id)
    items, kept, invalid, pending = review_decisions(
        json.loads(new_findings_json), rows
    )
    items.append(["summary", kept, invalid, pending, old_hash, new_hash])

    emit_report(serialize(items), output_path, stdout)
    return 0
