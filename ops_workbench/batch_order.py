"""Implementation of the ``batch-order`` subcommand.

Explains the provenance of stored batches as JSON Lines.  A batch stored
directly by ``audit-orders --batch`` lives in ``batches`` and is a source
batch; a batch stored by ``apply-fixes`` lives in ``derived_batches`` and
is a derived batch carrying its direct ``SOURCE`` batch id.  The
originating source batch is resolved by walking the ``source_batch_id``
chain until a batch outside ``derived_batches`` is reached; a chain that
runs into a missing row has no resolvable source.

Each derived batch additionally explains the impact of the corrections
applied to it: one ``["fix", ...]`` row per entry of the decision/proposal
snapshot saved by ``apply-fixes``, ordered by finding identity.  A
snapshot that is not valid JSON or whose entries miss their identity,
decision or proposal data is fatal before anything is emitted.  Only the
Python standard library is used.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import BinaryIO, TextIO

from .orders_audit import AuditError, emit_report, serialize

# Kind labels carried by every output row.
SOURCE = "source"
DERIVED = "derived"
FIX = "fix"

# Finding-identity type tags as used by ``diff-audits``.
_IDENTITY_TAGS = ("invalid", "duplicate", "conflict")


def _read_tables(
    db_path: str,
) -> tuple[dict[str, str], dict[str, tuple[str, str, str]]]:
    """Return ``(batches, derived)`` maps from a read-only database open.

    ``batches`` maps batch id to its ``input_sha256``; ``derived`` maps
    derived id to ``(input_sha256, direct source batch id, snapshot JSON
    text)``.  A database without either table simply yields an empty map
    for that table.  A missing database file or storage problem raises
    :class:`AuditError`.
    """
    if not os.path.isfile(db_path):
        raise AuditError("database does not exist", filename=db_path)
    batches: dict[str, str] = {}
    derived: dict[str, tuple[str, str, str]] = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            try:
                rows = conn.execute(
                    "SELECT batch_id, input_sha256 FROM batches"
                ).fetchall()
                batches = {batch_id: digest for batch_id, digest in rows}
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise AuditError(
                        f"cannot read database: {exc}", filename=db_path
                    )
            try:
                rows = conn.execute(
                    "SELECT derived_id, input_sha256, source_batch_id, "
                    "snapshot_json FROM derived_batches"
                ).fetchall()
                derived = {
                    derived_id: (digest, source_id, snapshot_json)
                    for derived_id, digest, source_id, snapshot_json in rows
                }
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise AuditError(
                        f"cannot read database: {exc}", filename=db_path
                    )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AuditError(f"cannot read database: {exc}", filename=db_path)
    return batches, derived


def _resolve_source(
    direct_source: str,
    batches: dict[str, str],
    derived: dict[str, tuple[str, str]],
) -> str | None:
    """Walk ``source_batch_id`` links up to the originating source batch.

    Each link target that is itself a derived batch is followed.  The first
    target outside ``derived_batches`` is the source when it exists in
    ``batches``.  A link to a batch present in neither table (including a
    chain through a deleted derived row) or a chain that loops without ever
    reaching a source resolves to ``None``.
    """
    seen: set[str] = set()
    current = direct_source
    while current in derived:
        if current in seen:
            return None
        seen.add(current)
        current = derived[current][1]
    return current if current in batches else None


def build_batch_order(
    batches: dict[str, str],
    derived: dict[str, tuple[str, str]],
    batch_ids: list[str],
) -> list[list]:
    """Build the provenance rows plus the trailing summary.

    Every id in ``batch_ids`` is assumed to exist in one of the two maps;
    the caller validates membership first so a missing id never produces
    partial output.  Rows follow the input order.  Each batch row is
    ``[batch id, kind, source batch id, derived batch id, input_sha256]``:
    source batches name themselves as the source and carry no derived id;
    derived batches carry their direct ``SOURCE`` batch id verbatim (even
    when that batch is gone) and the source reached by chain walking (or
    ``null``).  The final row is
    ``["summary", batch count, source count, derived count, hashes]`` with
    every row's hash deduplicated and sorted ascending.
    """
    rows: list[list] = []
    hashes: set[str] = set()
    source_count = 0
    derived_count = 0
    for batch_id in batch_ids:
        if batch_id in derived:
            digest, direct_source = derived[batch_id]
            rows.append(
                [
                    batch_id,
                    DERIVED,
                    _resolve_source(direct_source, batches, derived),
                    direct_source,
                    digest,
                ]
            )
            derived_count += 1
            hashes.add(digest)
        else:
            digest = batches[batch_id]
            rows.append([batch_id, SOURCE, batch_id, None, digest])
            source_count += 1
            hashes.add(digest)
    rows.append(
        [
            "summary",
            len(batch_ids),
            source_count,
            derived_count,
            sorted(hashes),
        ]
    )
    return rows


def _snapshot_error(derived_id: str, detail: str) -> AuditError:
    return AuditError(
        f"snapshot of derived batch {derived_id!r} is incomplete: {detail}",
        filename="<snapshot>",
    )


def _valid_identity(identity, derived_id: str) -> tuple:
    """Validate a snapshot finding identity; return its tuple sort key."""
    if not isinstance(identity, list) or len(identity) != 3:
        raise _snapshot_error(
            derived_id,
            "identity must be [\"invalid\",记录号,字段] or "
            "[\"duplicate\"|\"conflict\",order_id,sku]",
        )
    tag = identity[0]
    if tag not in _IDENTITY_TAGS or not isinstance(tag, str):
        raise _snapshot_error(
            derived_id,
            "identity type must be \"invalid\", \"duplicate\" or \"conflict\"",
        )
    if tag == "invalid":
        recno, field = identity[1], identity[2]
        if (
            not isinstance(recno, int)
            or isinstance(recno, bool)
            or recno < 1
            or not isinstance(field, str)
        ):
            raise _snapshot_error(
                derived_id,
                "an invalid identity must carry a positive record number and "
                "a field name",
            )
    else:
        oid, sku = identity[1], identity[2]
        if not isinstance(oid, str) or not isinstance(sku, str):
            raise _snapshot_error(
                derived_id,
                "a duplicate/conflict identity must carry an order id and sku",
            )
    return tuple(identity)


def _fix_rows_for_snapshot(derived_id: str, snapshot_text: str) -> list[list]:
    """Translate one derived batch snapshot into fix-impact rows.

    Returns the rows ``["fix", derived id, identity, [action, reason],
    values]`` ordered by finding identity; an empty snapshot yields no
    rows.  Every entry must carry its identity, its decision (action and
    trimmed reason) and a proposal whose PATCH is a non-empty list of
    ``[record number, field, new value]`` triples; otherwise
    :class:`AuditError` is raised so the whole run fails without partial
    output.
    """
    try:
        snapshot = json.loads(snapshot_text)
    except (json.JSONDecodeError, RecursionError, TypeError) as exc:
        raise AuditError(
            f"snapshot of derived batch {derived_id!r} is not valid JSON: {exc}",
            filename="<snapshot>",
        )
    if not isinstance(snapshot, list):
        raise _snapshot_error(derived_id, "snapshot must be a JSON array")

    parsed: list[tuple[tuple, list]] = []
    for index, entry in enumerate(snapshot):
        where = f"entry {index}"
        if not isinstance(entry, dict):
            raise _snapshot_error(derived_id, f"{where} must be a JSON object")
        if "identity" not in entry:
            raise _snapshot_error(derived_id, f"{where} is missing its identity")
        identity = entry["identity"]
        identity_key = _valid_identity(identity, derived_id)

        if "action" not in entry or "reason" not in entry:
            raise _snapshot_error(
                derived_id, f"{where} is missing its decision (action/reason)"
            )
        action = entry["action"]
        reason = entry["reason"]
        if not isinstance(action, str):
            raise _snapshot_error(derived_id, f"{where} action must be a string")
        if not isinstance(reason, str):
            raise _snapshot_error(derived_id, f"{where} reason must be a string")
        reason = reason.strip()
        if not reason:
            raise _snapshot_error(
                derived_id, f"{where} reason must not be empty after trimming"
            )

        if "proposal" not in entry:
            raise _snapshot_error(
                derived_id,
                f"{where} has a decision but no proposal (incomplete data)",
            )
        proposal = entry["proposal"]
        if not isinstance(proposal, dict) or "patch" not in proposal:
            raise _snapshot_error(
                derived_id, f"{where} proposal is missing its PATCH"
            )
        patch = proposal["patch"]
        if not isinstance(patch, list) or not patch:
            raise _snapshot_error(
                derived_id,
                f"{where} PATCH must be a non-empty array of "
                "[记录号,字段,新值] triples",
            )
        values: list = []
        for triple_index, triple in enumerate(patch):
            if (
                not isinstance(triple, list)
                or len(triple) != 3
                or not isinstance(triple[0], int)
                or isinstance(triple[0], bool)
                or triple[0] < 1
                or not isinstance(triple[1], str)
                or not isinstance(triple[2], str)
            ):
                raise _snapshot_error(
                    derived_id,
                    f"{where} PATCH item {triple_index} must be "
                    "[positive record number, field, string new value]",
                )
        # Merge every target cell of the finding, ordered by record number
        # and then field name, into one flat [记录号,字段,新值] sequence.
        for recno, field, new_value in sorted(
            patch, key=lambda triple: (triple[0], triple[1])
        ):
            values.extend([recno, field, new_value])

        parsed.append(
            (identity_key, [FIX, derived_id, identity, [action, reason], values])
        )

    # Identities with different type tags never compare past the tag
    # ("conflict"/"duplicate" sort before "invalid"), so the tuple order is
    # total across the homogeneous tail shapes.
    parsed.sort(key=lambda item: item[0])
    return [row for _key, row in parsed]


def build_order_with_fixes(
    batches: dict[str, str],
    derived: dict[str, tuple[str, str, str]],
    batch_ids: list[str],
) -> list[list]:
    """Build provenance rows, per-derived fix-impact rows and the summary.

    The provenance rows and the trailing summary are exactly the ones
    :func:`build_batch_order` produces (snapshots are invisible to that
    construction); each derived batch's ``["fix", ...]`` rows are inserted
    right after its provenance row and before the next batch's provenance
    row.  Every requested derived snapshot is parsed and validated while
    building, so a malformed or incomplete snapshot raises
    :class:`AuditError` before any row is emitted.
    """
    provenance = build_batch_order(
        batches,
        {did: (record[0], record[1]) for did, record in derived.items()},
        batch_ids,
    )
    fix_rows = {
        batch_id: _fix_rows_for_snapshot(batch_id, derived[batch_id][2])
        for batch_id in batch_ids
        if batch_id in derived
    }
    rows: list[list] = []
    for row, batch_id in zip(provenance[:-1], batch_ids):
        rows.append(row)
        rows.extend(fix_rows.get(batch_id, ()))
    rows.append(provenance[-1])
    return rows


def run_batch_order(
    db_path: str,
    batch_ids: list[str],
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Explain batch provenance and emit JSON Lines; return exit code.

    A missing database or a requested id present in neither table raises
    :class:`AuditError` (exit 2) before anything is written, as does a
    derived batch whose saved decision/proposal snapshot is malformed or
    incomplete, so no partial results are produced and the database is
    never modified.
    """
    batches, derived = _read_tables(db_path)
    for batch_id in batch_ids:
        if batch_id not in derived and batch_id not in batches:
            raise AuditError(f"batch {batch_id!r} not found", filename=db_path)
    rows = build_order_with_fixes(batches, derived, batch_ids)
    emit_report(serialize(rows), output_path, stdout)
    return 0
