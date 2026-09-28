"""Implementation of the ``batch-order`` subcommand.

Explains the provenance of stored batches as JSON Lines.  A batch stored
directly by ``audit-orders --batch`` lives in ``batches`` and is a source
batch; a batch stored by ``apply-fixes`` lives in ``derived_batches`` and
is a derived batch carrying its direct ``SOURCE`` batch id.  The
originating source batch is resolved by walking the ``source_batch_id``
chain until a batch outside ``derived_batches`` is reached; a chain that
runs into a missing row has no resolvable source.

Each derived batch additionally explains the impact of the corrections
applied to derive it: one ``"fix"`` row per entry of the decision/proposal
snapshot saved by ``apply-fixes``, ordered by finding identity, placed
right after that batch's provenance row.  A snapshot that cannot be
explained faithfully (invalid JSON, or an entry missing its identity,
decision or proposal) is a fatal error before anything is emitted.

When the run includes one or more derived batches, a cross-batch summary
is emitted before every provenance row: one ``"cell"`` row per cell
corrected at least once (listing every correction step in execution
order, with the final value), followed by one ``"chain-summary"`` row.
Only the Python standard library is used.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import BinaryIO, TextIO

from .orders_audit import AuditError, emit_report, serialize

# Kind labels carried by every provenance row.
SOURCE = "source"
DERIVED = "derived"


def _read_tables(
    db_path: str,
) -> tuple[dict[str, str], dict[str, tuple[str, str, str]]]:
    """Return ``(batches, derived)`` maps from a read-only database open.

    ``batches`` maps batch id to its ``input_sha256``; ``derived`` maps
    derived id to ``(input_sha256, direct source batch id, snapshot
    JSON)``.  A database without either table simply yields an empty map
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
    derived: dict[str, tuple],
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
    derived: dict[str, tuple],
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

    ``derived`` values may be ``(digest, direct source)`` pairs or
    ``(digest, direct source, snapshot)`` triples; only the first two
    elements matter here.  Fix-impact rows are built separately by
    :func:`build_fix_impact_rows` and interleave these provenance rows.
    """
    rows: list[list] = []
    hashes: set[str] = set()
    source_count = 0
    derived_count = 0
    for batch_id in batch_ids:
        if batch_id in derived:
            digest, direct_source = derived[batch_id][0], derived[batch_id][1]
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


def _parse_snapshot(
    derived_id: str, snapshot_text: str, db_path: str
) -> list[tuple[tuple, str, str, list[list]]]:
    """Parse and shape-check one derived batch's decision/fix snapshot.

    Returns entries as ``(identity tuple, action, trimmed reason, patch
    triples)`` in their stored order (callers sort by identity).  The
    snapshot is the traceability record written by ``apply-fixes``: a JSON
    array whose entries carry an ``identity``, the decision's ``action``
    and ``reason`` and the applied ``proposal`` (with its ``patch``).  Any
    structural incompleteness — invalid JSON, an entry that is not an
    object, or a missing/misshapen identity, decision or proposal field —
    raises :class:`AuditError` so the whole run fails before producing
    output rather than explaining a correction it cannot account for.
    """
    try:
        snapshot = json.loads(snapshot_text)
    except (json.JSONDecodeError, RecursionError, TypeError) as exc:
        raise AuditError(
            f"derived batch {derived_id!r} snapshot is not valid JSON: {exc}",
            filename=db_path,
        )
    if not isinstance(snapshot, list):
        raise AuditError(
            f"derived batch {derived_id!r} snapshot must be a JSON array",
            filename=db_path,
        )

    def fail(detail: str) -> None:
        raise AuditError(
            f"derived batch {derived_id!r} snapshot is incomplete: {detail}",
            filename=db_path,
        )

    entries: list[tuple[tuple, str, str, list[list]]] = []
    for index, entry in enumerate(snapshot):
        where = f"entry {index}"
        if not isinstance(entry, dict):
            fail(f"{where} is not a JSON object")
        identity = entry.get("identity")
        if not isinstance(identity, list) or len(identity) != 3:
            fail(f"{where} is missing a finding identity")
        kind = identity[0]
        if kind == "invalid":
            recno, field = identity[1], identity[2]
            if (
                not isinstance(recno, int)
                or isinstance(recno, bool)
                or recno < 1
                or not isinstance(field, str)
            ):
                fail(f"{where} has a malformed invalid identity")
        elif kind in ("duplicate", "conflict"):
            if not isinstance(identity[1], str) or not isinstance(identity[2], str):
                fail(f"{where} has a malformed {kind} identity")
        else:
            fail(f"{where} has an unknown identity kind {kind!r}")

        action = entry.get("action")
        reason = entry.get("reason")
        if not isinstance(action, str) or not isinstance(reason, str):
            fail(f"{where} is missing its decision action or reason")

        proposal = entry.get("proposal")
        if not isinstance(proposal, dict) or "patch" not in proposal:
            fail(f"{where} is missing its fix proposal")
        patch = proposal["patch"]
        if not isinstance(patch, list):
            fail(f"{where} proposal patch must be a JSON array")
        triples: list[list] = []
        for t_index, triple in enumerate(patch):
            if (
                not isinstance(triple, list)
                or len(triple) != 3
                or not isinstance(triple[0], int)
                or isinstance(triple[0], bool)
                or triple[0] < 1
                or not isinstance(triple[1], str)
                or not isinstance(triple[2], str)
            ):
                fail(
                    f"{where} patch item {t_index} must be "
                    "[record number, field, new value]"
                )
            triples.append([triple[0], triple[1], triple[2]])
        entries.append((tuple(identity), action, reason.strip(), triples))
    return entries


def build_fix_impact_rows(
    derived_id: str,
    entries: list[tuple[tuple, str, str, list[list]]],
) -> list[list]:
    """Build the ``"fix"`` impact rows of one derived batch.

    One row per snapshot entry, identities ascending.  Each row is
    ``["fix", derived batch id, identity, [action, reason], values]`` where
    ``values`` lists every proposed target cell of that entry as
    ``[record number, field, new value]`` triples sorted by record number
    and then field name.  An empty entry list yields no rows.
    """
    rows: list[list] = []
    for identity, action, reason, triples in sorted(entries, key=lambda e: e[0]):
        values = sorted(
            ([recno, field, new_value] for recno, field, new_value in triples),
            key=lambda triple: (triple[0], triple[1]),
        )
        rows.append(
            ["fix", derived_id, list(identity), [action, reason], values]
        )
    return rows


def build_correction_summary_rows(
    ordered_derived: list[tuple[str, list[tuple]]],
) -> list[list]:
    """Build the cross-batch correction summary rows.

    ``ordered_derived`` lists ``(derived batch id, parsed snapshot entries)``
    for the distinct derived batches in their first-input-occurrence order.

    The corrections are flattened into one global execution sequence: the
    derived batches run in their input order, within a batch the snapshot
    entries run by ascending identity, and within an entry the new-value
    triples run by record number then field name.  Every target cell of
    every triple is one correction step shaped
    ``[derived batch id, identity, [action, reason], new value]``; a derived
    batch whose snapshot contributes no triples adds no steps.

    For each cell corrected at least once a
    ``["cell", record number, field, steps, final value]`` row is emitted:
    ``steps`` lists every step that touched the cell in execution order and
    ``final value`` is the new value of the last step.  Cell rows are
    ordered by record number then field name.  The closing row is
    ``["chain-summary", corrected cell count, total step count, repeatedly
    modified cell count, final-value source batch count]``: a cell is
    repeatedly modified when it occurs more than once in the execution
    sequence, and the source batch count is the number of distinct derived
    batches owning the cells' final steps.  With no correction steps at all
    only the all-zero ``"chain-summary"`` row is returned.
    """
    # One correction step per target cell, in global execution order.
    steps: list[tuple[int, str, list]] = []
    for derived_id, entries in ordered_derived:
        for identity, action, reason, triples in sorted(
            entries, key=lambda entry: entry[0]
        ):
            decision = [action, reason]
            for recno, field, new_value in sorted(
                triples, key=lambda triple: (triple[0], triple[1])
            ):
                steps.append(
                    (
                        recno,
                        field,
                        [derived_id, list(identity), decision, new_value],
                    )
                )

    by_cell: dict[tuple[int, str], list[list]] = {}
    for recno, field, step in steps:
        by_cell.setdefault((recno, field), []).append(step)

    rows: list[list] = []
    repeated = 0
    final_sources: set[str] = set()
    for recno, field in sorted(by_cell):
        cell_steps = by_cell[(recno, field)]
        if len(cell_steps) > 1:
            repeated += 1
        final_sources.add(cell_steps[-1][0])
        rows.append(["cell", recno, field, cell_steps, cell_steps[-1][3]])
    rows.append(
        ["chain-summary", len(by_cell), len(steps), repeated, len(final_sources)]
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
    results are produced and the database is never modified.  A derived
    batch whose saved snapshot is invalid JSON or structurally incomplete
    fails the same way before the report is generated.  When the run
    contains a derived batch, cross-batch correction summary rows (see
    :func:`build_correction_summary_rows`) lead the report; they never
    affect the provenance rows or the trailing summary.
    """
    batches, derived = _read_tables(db_path)
    for batch_id in batch_ids:
        if batch_id not in derived and batch_id not in batches:
            raise AuditError(f"batch {batch_id!r} not found", filename=db_path)

    # Parse every snapshot before building output: one corrupt snapshot
    # anywhere in the run fails the whole invocation without partial rows
    # and without touching an existing --output file.
    snapshots: dict[str, list[tuple]] = {}
    for batch_id in batch_ids:
        if batch_id in derived and batch_id not in snapshots:
            snapshots[batch_id] = _parse_snapshot(
                batch_id, derived[batch_id][2], db_path
            )

    provenance = build_batch_order(batches, derived, batch_ids)
    body_rows: list[list] = []
    for row in provenance[:-1]:
        body_rows.append(row)
        if row[1] == DERIVED:
            # Fix-impact rows sit directly behind this batch's provenance
            # row and before the next batch's provenance row.  They never
            # contribute to the trailing summary.
            body_rows.extend(build_fix_impact_rows(row[0], snapshots[row[0]]))

    # The cross-batch correction summary, when derived batches are in play,
    # leads the report ahead of every provenance row.  Each distinct
    # derived batch contributes once, in its first input-occurrence order;
    # the snapshots dict was populated the same way above.
    rows: list[list] = []
    if any(batch_id in derived for batch_id in batch_ids):
        ordered_derived = [
            (batch_id, snapshots[batch_id])
            for batch_id in dict.fromkeys(batch_ids)
            if batch_id in derived
        ]
        rows.extend(build_correction_summary_rows(ordered_derived))
    rows.extend(body_rows)
    rows.append(provenance[-1])

    emit_report(serialize(rows), output_path, stdout)
    return 0
