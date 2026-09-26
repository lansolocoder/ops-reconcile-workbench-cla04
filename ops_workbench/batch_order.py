"""Implementation of the ``batch-order`` subcommand.

Explains the provenance of stored batches as JSON Lines.  A batch stored
directly by ``audit-orders --batch`` lives in ``batches`` and is a source
batch; a batch stored by ``apply-fixes`` lives in ``derived_batches`` and
is a derived batch carrying its direct ``SOURCE`` batch id.  The
originating source batch is resolved by walking the ``source_batch_id``
chain until a batch outside ``derived_batches`` is reached; a chain that
runs into a missing row has no resolvable source.  Only the Python
standard library is used.
"""

from __future__ import annotations

import os
import sqlite3
from typing import BinaryIO, TextIO

from .orders_audit import AuditError, emit_report, serialize

# Kind labels carried by every output row.
SOURCE = "source"
DERIVED = "derived"


def _read_tables(db_path: str) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Return ``(batches, derived)`` maps from a read-only database open.

    ``batches`` maps batch id to its ``input_sha256``; ``derived`` maps
    derived id to ``(input_sha256, direct source batch id)``.  A database
    without either table simply yields an empty map for that table.  A
    missing database file or storage problem raises :class:`AuditError`.
    """
    if not os.path.isfile(db_path):
        raise AuditError("database does not exist", filename=db_path)
    batches: dict[str, str] = {}
    derived: dict[str, tuple[str, str]] = {}
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
                    "SELECT derived_id, input_sha256, source_batch_id "
                    "FROM derived_batches"
                ).fetchall()
                derived = {
                    derived_id: (digest, source_id)
                    for derived_id, digest, source_id in rows
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


def run_batch_order(
    db_path: str,
    batch_ids: list[str],
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Explain batch provenance and emit JSON Lines; return exit code.

    A missing database or a requested id present in neither table raises
    :class:`AuditError` (exit 2) before anything is written, so no partial
    results are produced and the database is never modified.
    """
    batches, derived = _read_tables(db_path)
    for batch_id in batch_ids:
        if batch_id not in derived and batch_id not in batches:
            raise AuditError(f"batch {batch_id!r} not found", filename=db_path)
    rows = build_batch_order(batches, derived, batch_ids)
    emit_report(serialize(rows), output_path, stdout)
    return 0
