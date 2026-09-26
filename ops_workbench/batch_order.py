"""Implementation of the ``batch-order`` subcommand.

Explains the provenance of stored batches in the caller's business order:
batches written by ``audit-orders --db/--batch`` are *source* batches and
batches written by ``apply-fixes`` (table ``derived_batches``, which records
the immediate ``SOURCE`` batch id) are *derived* batches.  For every
requested batch id one JSON Lines item explains where the batch comes from;
a trailing ``summary`` item aggregates the run.  Only the Python standard
library is used.
"""

from __future__ import annotations

import os
import sqlite3
from typing import BinaryIO, TextIO

from .orders_audit import AuditError, emit_report, serialize


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _fetch_lineage(
    db_path: str,
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Read the lineage tables.

    Returns ``(sources, derived)``: ``sources`` maps a source batch id to
    its stored ``input_sha256``; ``derived`` maps a derived batch id to
    ``(input_sha256, source_batch_id)``.  A missing table reads as empty.
    """
    if not os.path.isfile(db_path):
        raise AuditError("database does not exist", filename=db_path)
    try:
        conn = sqlite3.connect(db_path)
        try:
            sources: dict[str, str] = {}
            if _table_exists(conn, "batches"):
                rows = conn.execute(
                    "SELECT batch_id, input_sha256 FROM batches"
                ).fetchall()
                sources = {batch_id: sha for batch_id, sha in rows}
            derived: dict[str, tuple[str, str]] = {}
            if _table_exists(conn, "derived_batches"):
                rows = conn.execute(
                    "SELECT derived_id, input_sha256, source_batch_id "
                    "FROM derived_batches"
                ).fetchall()
                derived = {
                    derived_id: (sha, source_id)
                    for derived_id, sha, source_id in rows
                }
            return sources, derived
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AuditError(f"cannot read database: {exc}", filename=db_path)


def _resolve_origin(
    parent_id: str,
    sources: dict[str, str],
    derived: dict[str, tuple[str, str]],
) -> str | None:
    """Walk the ``source_batch_id`` chain up to the ultimate source batch.

    Returns the id of the first batch on the chain that is not itself
    derived, or ``None`` when the chain points at a derived batch row that
    is lost, at a batch that does not exist at all, or loops back on
    itself.
    """
    node = parent_id
    seen = set()
    while node in derived and node not in seen:
        seen.add(node)
        node = derived[node][1]
    if node in derived or node not in sources:
        return None
    return node


def run_batch_order(
    db_path: str,
    batch_ids: list[str],
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Explain the provenance of ``batch_ids`` in the given order; exit code.

    Each requested id yields one item
    ``[batch id, kind, source id, derived-from id, input_sha256]`` where
    ``kind`` is ``"source"`` or ``"derived"``.  A derived batch's
    derived-from id is its immediate ``SOURCE`` batch id verbatim, even
    when that batch no longer exists; its source id is found by walking
    the ``source_batch_id`` chain up to the first non-derived batch and
    is ``null`` when the chain is broken.  A source batch's source id is
    itself and its derived-from id is ``null``.  The trailing
    ``["summary", batches, sources, derived, hashes]`` item counts the
    emitted kinds and lists every distinct ``input_sha256`` ascending.

    Any unknown batch id, a missing database or fewer than two batch ids
    raises :class:`AuditError` (exit 2) before anything is written, so no
    partial output is produced and the database is never modified.
    """
    if len(batch_ids) < 2:
        raise AuditError("at least two batch IDs are required")

    sources, derived = _fetch_lineage(db_path)

    items: list[list] = []
    for batch_id in batch_ids:
        if batch_id in derived:
            sha, parent_id = derived[batch_id]
            origin = _resolve_origin(parent_id, sources, derived)
            items.append([batch_id, "derived", origin, parent_id, sha])
        elif batch_id in sources:
            items.append([batch_id, "source", batch_id, None, sources[batch_id]])
        else:
            raise AuditError(f"batch {batch_id!r} not found", filename=db_path)

    source_count = sum(1 for item in items if item[1] == "source")
    derived_count = len(items) - source_count
    hashes = sorted({item[4] for item in items})
    items.append(["summary", len(items), source_count, derived_count, hashes])

    emit_report(serialize(items), output_path, stdout)
    return 0
