"""Traceable corrections: ``propose-fix`` and ``apply-fixes``.

``propose-fix`` attaches a validated cell-level PATCH to a finding whose
recorded decision is ``fix``.  ``apply-fixes`` replays all proposals of a
source batch onto a CSV whose bytes match the source batch, re-audits the
result under the source schema and traces the outcome as a derived batch.
Only the Python standard library is used.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from typing import BinaryIO, TextIO

from .diff_audits import finding_identity
from .orders_audit import (
    AuditError,
    FIELDS,
    _canonical_json,
    _field_valid,
    audit,
    emit_report,
    fetch_batch,
    resolve_columns,
    serialize,
)

_CREATE_PROPOSALS_SQL = """
CREATE TABLE IF NOT EXISTS fix_proposals (
    batch_id TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    finding_json TEXT NOT NULL,
    patch_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, identity_json)
)
"""

_CREATE_DERIVED_SQL = """
CREATE TABLE IF NOT EXISTS derived_batches (
    derived_id TEXT PRIMARY KEY,
    source_batch_id TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
)
"""


class FixConflictError(Exception):
    """A proposal/derived batch id is stored with different content (exit 3).

    The database and any pre-existing output are left untouched.
    """

    def __init__(self, kind: str, identifier: str):
        self.kind = kind
        self.identifier = identifier
        if kind == "proposal":
            message = (
                f"a fix proposal for identity {identifier} is already stored "
                f"with a different patch"
            )
        else:
            message = (
                f"derived batch {identifier!r} is already stored with different "
                f"content"
            )
        super().__init__(message)


@contextlib.contextmanager
def _fix_transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """Hold one immediate SQLite transaction, creating the fix tables.

    Storage errors are reported as :class:`AuditError` (exit 2); anything
    raised inside the block rolls the transaction back, so a failed command
    never changes a stored proposal or derived batch.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.isolation_level = None  # explicit BEGIN/COMMIT below
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_CREATE_PROPOSALS_SQL)
        conn.execute(_CREATE_DERIVED_SQL)
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
            raise AuditError(f"cannot commit: {exc}", filename=db_path)
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    # ``bool`` is a subclass of int and must not count.
    return isinstance(value, int) and not isinstance(value, bool)


def parse_patch(raw: str) -> list[list]:
    """Parse the PATCH argument into ``[[record number, field, new value], ...]``.

    Structure only is checked here; the cell targets and values are checked
    against the finding by :func:`validate_patch`.  Raises :class:`AuditError`
    for any malformed patch (exit 2).
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"PATCH is not valid JSON: {exc}", filename="PATCH")
    if not isinstance(obj, list) or not obj:
        raise AuditError("PATCH must be a non-empty JSON array", filename="PATCH")
    patch: list[list] = []
    for pos, item in enumerate(obj):
        if not isinstance(item, list) or len(item) != 3:
            raise AuditError(
                f"PATCH item {pos} must be a [record number, field, new value] "
                f"triple",
                filename="PATCH",
            )
        rec, field, value = item
        if not _is_int(rec) or rec <= 0:
            raise AuditError(
                f"PATCH item {pos}: record number must be a positive integer",
                filename="PATCH",
            )
        if field not in FIELDS:
            raise AuditError(
                f"PATCH item {pos}: field must be one of {', '.join(FIELDS)}, "
                f"got {field!r}",
                filename="PATCH",
            )
        if not isinstance(value, str):
            raise AuditError(
                f"PATCH item {pos}: new value must be a JSON string",
                filename="PATCH",
            )
        patch.append([rec, field, value])

    targets = [(rec, field) for rec, field, _ in patch]
    if len(set(targets)) != len(targets):
        raise AuditError(
            "PATCH targets the same record/field cell more than once",
            filename="PATCH",
        )
    return patch


def validate_patch(patch: list[list], finding: list) -> None:
    """Check a structurally valid patch against one stored finding.

    An ``invalid`` finding only permits the single cell named by its
    identity; a ``duplicate``/``conflict`` finding only permits records it
    lists.  Every new value must pass the corresponding logical field
    validation.  Raises :class:`AuditError` (exit 2) on the first violation.
    """
    kind = finding[0]
    if kind == "invalid":
        rec, field = finding[1], finding[2]
        allowed = {(rec, field)}
        scope = f"record {rec} field {field!r}"
    else:
        allowed = {(rec, field) for rec in finding[2] for field in FIELDS}
        scope = f"records {finding[2]} of the {kind} finding"
    for target_rec, target_field, new_value in patch:
        if (target_rec, target_field) not in allowed:
            raise AuditError(
                f"target record {target_rec} field {target_field!r} is outside "
                f"the finding scope ({scope})",
                filename="PATCH",
            )
        ok, _ = _field_valid(target_field, new_value)
        if not ok:
            raise AuditError(
                f"new value {new_value!r} for field {target_field!r} fails "
                f"field validation",
                filename="PATCH",
            )


def _load_decisions(
    conn: sqlite3.Connection, batch_id: str
) -> list[tuple[str, str, str, str]]:
    """Return ``(identity, action, reason, finding)`` rows of one batch."""
    try:
        return conn.execute(
            "SELECT identity_json, action, reason, finding_json "
            "FROM decisions WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def _load_proposals(
    conn: sqlite3.Connection, batch_id: str
) -> list[tuple[str, str, str]]:
    """Return ``(identity, finding, patch)`` rows stored for one batch."""
    try:
        return conn.execute(
            "SELECT identity_json, finding_json, patch_json "
            "FROM fix_proposals WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


# ---------------------------------------------------------------------------
# propose-fix
# ---------------------------------------------------------------------------


def run_propose_fix(
    db_path: str,
    batch_id: str,
    identity_text: str,
    patch_text: str,
) -> int:
    """Store a validated fix proposal; return exit code.

    The batch must exist, ``identity_text`` must match one of its stored
    findings, the finding must carry a ``fix`` decision whose stored finding
    still matches, and the patch must be well-formed, in scope and valid.
    Repeating the exact same proposal is idempotent (exit 0); the same
    identity with a different patch raises :class:`FixConflictError`
    (exit 3).  Any other problem raises :class:`AuditError` (exit 2) and the
    database is never changed.
    """
    patch = parse_patch(patch_text)
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
            f"identity {ident} does not match any finding of batch "
            f"{batch_id!r}",
            filename="ID",
        )
    finding = matches[0]
    validate_patch(patch, finding)

    identity_json = _canonical_json(identity)
    finding_json = _canonical_json(finding)
    patch_json = _canonical_json(patch)
    with _fix_transaction(db_path) as conn:
        decision = None
        if _table_exists(conn, "decisions"):
            decision = conn.execute(
                "SELECT action, reason, finding_json FROM decisions "
                "WHERE batch_id = ? AND identity_json = ?",
                (batch_id, identity_json),
            ).fetchone()
        if decision is None:
            raise AuditError(
                f"no fix decision recorded for identity {identity_json} in "
                f"batch {batch_id!r}",
                filename="ID",
            )
        action, _reason, decision_finding_json = decision
        if action != "fix":
            raise AuditError(
                f"the decision for identity {identity_json} is {action!r}, not "
                f"'fix'",
                filename="ID",
            )
        if decision_finding_json != finding_json:
            raise AuditError(
                f"the stored finding of the decision no longer matches the "
                f"batch finding for identity {identity_json}",
                filename="ID",
            )

        existing = conn.execute(
            "SELECT patch_json FROM fix_proposals "
            "WHERE batch_id = ? AND identity_json = ?",
            (batch_id, identity_json),
        ).fetchone()
        if existing is not None:
            if existing[0] != patch_json:
                raise FixConflictError("proposal", identity_json)
            # Identical proposal: idempotent, the database is not rewritten.
        else:
            conn.execute(
                "INSERT INTO fix_proposals "
                "(batch_id, identity_json, finding_json, patch_json) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, identity_json, finding_json, patch_json),
            )
    return 0


# ---------------------------------------------------------------------------
# CSV parsing and patching
# ---------------------------------------------------------------------------


def _read_csv(
    data: bytes, input_name: str
) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """Parse input bytes into a header and every logical record.

    Records are ``(logical_record_number, cells)``; a blank physical line is
    delivered as the empty list (and still carries its record number).  The
    header is record 1.  Raises :class:`AuditError` for encoding/CSV errors.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AuditError(
            f"input is not valid UTF-8: {exc.reason} at byte {exc.start}",
            filename=input_name,
        )

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise AuditError("CSV is empty (no header)", filename=input_name)
    except csv.Error as exc:
        raise AuditError(f"malformed CSV: {exc}", filename=input_name)

    records: list[tuple[int, list[str]]] = []
    record_no = 1
    while True:
        start_line = reader.line_num + 1
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise AuditError(
                f"malformed CSV near line {start_line}: {exc}",
                filename=input_name,
            )
        record_no += 1
        records.append((record_no, raw))
    return header, records


def apply_patches(
    data: bytes,
    schema: dict[str, list[str]],
    edits: list[tuple[int, str, str]],
    input_name: str,
) -> bytes:
    """Apply sorted cell edits and return the new UTF-8 CSV payload.

    Only targeted cells change; the header, column order, extra columns and
    every other cell keep their parsed value.  The output is written with
    ``\\n`` line endings and no BOM.  Blank inter-record lines keep their
    record number (an empty row serializes back to an empty line).
    """
    header, records = _read_csv(data, input_name)
    columns = resolve_columns(schema, header)
    rows: dict[int, list[str]] = {}
    for rec_no, raw in records:
        rows[rec_no] = raw

    for rec_no, field, new_value in edits:
        row = rows.get(rec_no)
        if row is None or not row or (len(row) == 1 and row[0].strip() == ""):
            raise AuditError(
                f"patch targets record {rec_no}, which is not a data record",
                filename="PATCH",
            )
        if len(row) != len(header):
            raise AuditError(
                f"record {rec_no} has {len(row)} fields but the header has "
                f"{len(header)}",
                filename=input_name,
            )
        row[columns[field]] = new_value

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for _rec_no, raw in records:
        writer.writerow(raw)
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# apply-fixes
# ---------------------------------------------------------------------------


def _stage_temp(path: str, payload: bytes) -> str:
    """Write ``payload`` to a temp file next to ``path`` and return its name.

    Nothing replaces ``path`` yet; a failure cleans the temp file up and
    leaves the destination untouched.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
        )
    except OSError as exc:
        raise AuditError(f"cannot prepare output: {exc}", filename=path)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise AuditError(f"cannot write output: {exc}", filename=path)
    return tmp_path


def _snapshot(
    decisions: list[tuple[str, str, str, str]],
    proposals: list[tuple[str, str, str]],
) -> list[list]:
    """Build the stored snapshot of SOURCE decisions and proposals."""
    return [
        [
            [json.loads(ident), action, reason, json.loads(finding)]
            for ident, action, reason, finding in decisions
        ],
        [
            [json.loads(ident), json.loads(finding), json.loads(patch)]
            for ident, finding, patch in proposals
        ],
    ]


def _ordered_edits(
    proposal_rows: list[tuple[list, list, list[list]]],
) -> list[tuple[int, str, str]]:
    """Flatten proposals to edits sorted by identity, record and field.

    Every proposal is ``(identity, finding, patch)``.  The identity tuple is
    the primary key (its first element is the finding type tag, so element
    types never mix), then the record number and finally the logical field.
    """
    ordered: list[tuple] = []
    for ident, _finding, patch in proposal_rows:
        for rec, field, value in patch:
            ordered.append((tuple(ident), rec, field, value))
    ordered.sort(key=lambda e: (e[0], e[1], e[2]))
    return [(rec, field, value) for _ident, rec, field, value in ordered]


def run_apply_fixes(
    db_path: str,
    source_id: str,
    derived_id: str,
    input_path: str,
    output_path: str,
    report_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Apply every proposal of SOURCE to INPUT and trace the derived batch.

    INPUT must hash to SOURCE's stored input hash; every ``fix`` decision of
    SOURCE must have a proposal.  Edits are applied in identity, record and
    field order and the resulting CSV is re-audited under the SOURCE schema.
    The fixed CSV is atomically written to ``output_path`` (INPUT is never
    modified) and the audit JSONL goes to stdout or ``report_path``.  The
    derived batch stores the new hash, schema, finding set, SOURCE id and a
    snapshot of SOURCE decisions/proposals.

    Re-running with the same DERIVED id and the same content is idempotent; a
    conflicting DERIVED id raises :class:`FixConflictError` (exit 3).  Stale
    decisions, illegal patches and any read/write, database or audit error
    raise :class:`AuditError` (exit 2); the database, DERIVED record and old
    outputs are left untouched and temporary files are cleaned up.
    """
    if os.path.abspath(output_path) == os.path.abspath(input_path):
        raise AuditError(
            "OUTPUT must not be the same file as INPUT; INPUT is never modified",
            filename=output_path,
        )

    source = fetch_batch(db_path, source_id)
    if source is None:
        raise AuditError(f"batch {source_id!r} not found", filename=db_path)
    source_hash, source_schema_json, source_findings_json = source
    source_findings = json.loads(source_findings_json)
    schema = json.loads(source_schema_json)

    try:
        with open(input_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise AuditError(f"cannot read input file: {exc}", filename=input_path)
    if hashlib.sha256(data).hexdigest() != source_hash:
        raise AuditError(
            f"INPUT hash does not match the input hash stored for source batch "
            f"{source_id!r}",
            filename=input_path,
        )

    # All validation and generation happens before any output file or stored
    # batch is touched.
    staged: list[str] = []  # temp paths not yet published
    try:
        with _fix_transaction(db_path) as conn:
            decisions = _load_decisions(conn, source_id)
            proposals = _load_proposals(conn, source_id)

            findings_by_id = {
                tuple(finding_identity(f)): f for f in source_findings
            }

            # Every proposal must still match a source finding and its fix
            # decision (a stale proposal is a fatal error).
            proposal_rows: list[tuple[list, list, list[list]]] = []
            for ident_json, finding_json, patch_json in proposals:
                ident = json.loads(ident_json)
                ident_tuple = tuple(ident)
                stored_finding = findings_by_id.get(ident_tuple)
                if (
                    stored_finding is None
                    or _canonical_json(stored_finding) != finding_json
                ):
                    raise AuditError(
                        f"stored proposal for {ident_json} no longer matches a "
                        f"finding of source batch {source_id!r}",
                        filename=db_path,
                    )
                decision = next(
                    (
                        (action, reason, finding)
                        for d_ident, action, reason, finding in decisions
                        if json.loads(d_ident) == ident
                    ),
                    None,
                )
                if decision is None or decision[0] != "fix":
                    raise AuditError(
                        f"proposal for {ident_json} has no matching fix decision "
                        f"in batch {source_id!r}",
                        filename=db_path,
                    )
                proposal_rows.append(
                    (ident, stored_finding, json.loads(patch_json))
                )

            # Every fix decision must have a proposal.
            proposed_ids = {
                tuple(json.loads(ident_json))
                for ident_json, _f, _p in proposals
            }
            for ident_json, action, _reason, decision_finding_json in decisions:
                ident_tuple = tuple(json.loads(ident_json))
                if action == "fix":
                    current = findings_by_id.get(ident_tuple)
                    if (
                        current is None
                        or _canonical_json(current) != decision_finding_json
                    ):
                        raise AuditError(
                            f"the fix decision for {ident_json} no longer "
                            f"matches a finding of source batch {source_id!r}",
                            filename=db_path,
                        )
                    if ident_tuple not in proposed_ids:
                        raise AuditError(
                            f"the fix decision for {ident_json} has no proposal",
                            filename=db_path,
                        )

            # Apply order: identity, then record number, then logical field.
            for _ident, finding, patch in proposal_rows:
                validate_patch(patch, finding)
            edits = _ordered_edits(proposal_rows)
            targets = [(rec, field) for rec, field, _ in edits]
            if len(set(targets)) != len(targets):
                raise AuditError(
                    "the proposals target the same record/field cell more than "
                    "once",
                    filename="PATCH",
                )

            new_data = apply_patches(data, schema, edits, input_path)
            new_findings, _ = audit(new_data, json.dumps(schema), output_path)
            payload = serialize(new_findings)
            new_hash = hashlib.sha256(new_data).hexdigest()

            snapshot_json = _canonical_json(_snapshot(decisions, proposals))
            record = (
                source_id,
                new_hash,
                source_schema_json,
                _canonical_json(new_findings[:-1]),
                source_hash,
                snapshot_json,
            )
            existing = conn.execute(
                "SELECT source_batch_id, input_sha256, schema_json, "
                "findings_json, source_sha256, snapshot_json "
                "FROM derived_batches WHERE derived_id = ?",
                (derived_id,),
            ).fetchone()
            if existing is not None:
                if existing != record:
                    raise FixConflictError("derived", derived_id)
                # Identical derived batch: idempotent; the stored row is kept.
            else:
                conn.execute(
                    "INSERT INTO derived_batches "
                    "(derived_id, source_batch_id, input_sha256, schema_json, "
                    "findings_json, source_sha256, snapshot_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (derived_id, *record),
                )

            # Stage both file outputs first: staging performs the full write
            # (and fails on a bad directory or a full disk) before anything is
            # replaced, so such errors roll the stored batch back.  A stdout
            # report is emitted before any file is replaced, so a broken pipe
            # cannot leave a published CSV behind.
            csv_tmp = _stage_temp(output_path, new_data)
            staged.append(csv_tmp)
            if report_path is None:
                emit_report(payload, None, stdout)
            else:
                report_tmp = _stage_temp(report_path, payload)
                staged.append(report_tmp)

            # Publish the fully generated files before COMMIT: an unexpected
            # replace error rolls the stored batch back.  INPUT is never
            # replaced and stays byte-identical.
            try:
                os.replace(csv_tmp, output_path)
                staged.remove(csv_tmp)
                if report_path is not None:
                    os.replace(report_tmp, report_path)
                    staged.remove(report_tmp)
            except OSError as exc:
                raise AuditError(f"cannot write output: {exc}", filename=output_path)
    except BaseException:
        for tmp_path in staged:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        raise
    return 0
