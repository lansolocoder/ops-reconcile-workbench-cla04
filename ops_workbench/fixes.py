"""Traceable corrections: ``propose-fix`` and ``apply-fixes``.

``propose-fix`` attaches a validated JSON patch (a non-empty array of
``[record number, logical field, new value]`` triples) to one ``fix``
decision of a batch stored by ``audit-orders --db/--batch``.  The original
finding and the patch are stored together.

``apply-fixes`` applies the proposals of one source batch to a matching
input CSV, re-audits the corrected rows with the source batch schema, and
traces the result as a derived batch.  Only the Python standard library is
used.
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
    FIELDS,
    AuditError,
    _canonical_json,
    _field_valid,
    audit,
    emit_report,
    fetch_batch,
    resolve_columns,
    serialize,
)


class FixConflictError(Exception):
    """A proposal/derived batch with the same id exists with other content.

    Exit status 3; the database and any pre-existing output are untouched.
    """


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
    input_sha256 TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    source_batch_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
)
"""


@contextlib.contextmanager
def _fix_transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """Hold one immediate SQLite transaction, creating the fix tables.

    Storage errors are reported as :class:`AuditError` (exit 2); anything
    raised inside the block rolls the transaction back, so a failed write
    never leaves a partial proposal or derived batch.
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


def _fetch_fix_decisions(
    conn: sqlite3.Connection, batch_id: str
) -> dict[tuple, tuple[str, str, str]]:
    """Return the batch's ``fix`` decisions keyed by identity tuple.

    Each value is ``(action, reason, finding_json)``.  A database without a
    decisions table simply has no decisions.
    """
    if not _table_exists(conn, "decisions"):
        return {}
    rows = conn.execute(
        "SELECT identity_json, action, reason, finding_json FROM decisions "
        "WHERE batch_id = ? AND action = 'fix'",
        (batch_id,),
    ).fetchall()
    return {
        tuple(json.loads(identity_json)): (action, reason, finding_json)
        for identity_json, action, reason, finding_json in rows
    }


def _fetch_all_decision_identities(
    conn: sqlite3.Connection, batch_id: str
) -> set[tuple]:
    """Identities of every decision recorded for the batch."""
    if not _table_exists(conn, "decisions"):
        return set()
    rows = conn.execute(
        "SELECT identity_json FROM decisions WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    return {tuple(json.loads(r[0])) for r in rows}


def _fetch_proposals(
    conn: sqlite3.Connection, batch_id: str
) -> dict[tuple, tuple[str, str]]:
    """Stored proposals keyed by identity: ``(finding_json, patch_json)``."""
    if not _table_exists(conn, "fix_proposals"):
        return {}
    rows = conn.execute(
        "SELECT identity_json, finding_json, patch_json FROM fix_proposals "
        "WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    return {
        tuple(json.loads(identity_json)): (finding_json, patch_json)
        for identity_json, finding_json, patch_json in rows
    }


def _ident_text(ident_tuple: tuple) -> str:
    return json.dumps(list(ident_tuple), ensure_ascii=False, separators=(",", ":"))


def _validate_identity(identity) -> tuple:
    """Check the shape shared by finding identities; return its tuple form."""
    if not isinstance(identity, list) or len(identity) != 3:
        raise AuditError(
            "ID must be a finding identity: [\"invalid\",记录号,字段] or "
            "[\"duplicate\"|\"conflict\",order_id,sku]",
            filename="ID",
        )
    kind = identity[0]
    if kind == "invalid":
        recno, field = identity[1], identity[2]
        if not isinstance(recno, int) or isinstance(recno, bool) or recno < 1:
            raise AuditError(
                "an invalid identity's record number must be a positive integer",
                filename="ID",
            )
        if field not in FIELDS:
            raise AuditError(
                f"an invalid identity's field must be one of "
                f"{', '.join(FIELDS)}, got {field!r}",
                filename="ID",
            )
    elif kind in ("duplicate", "conflict"):
        oid, sku = identity[1], identity[2]
        if not isinstance(oid, str) or not isinstance(sku, str):
            raise AuditError(
                "a duplicate/conflict identity's order id and sku must be strings",
                filename="ID",
            )
    else:
        raise AuditError(
            'ID type must be "invalid", "duplicate" or "conflict"',
            filename="ID",
        )
    return tuple(identity)


def _parse_patch(patch_text: str) -> list[list]:
    """Parse and shape-check PATCH; return the triple list."""
    try:
        patch = json.loads(patch_text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"PATCH is not valid JSON: {exc}", filename="PATCH")
    if not isinstance(patch, list) or not patch:
        raise AuditError(
            "PATCH must be a non-empty JSON array of [记录号,字段,新值] triples",
            filename="PATCH",
        )
    normalized: list[list] = []
    for index, triple in enumerate(patch):
        if (
            not isinstance(triple, list)
            or len(triple) != 3
            or not isinstance(triple[0], int)
            or isinstance(triple[0], bool)
            or triple[0] < 1
            or not isinstance(triple[2], str)
        ):
            raise AuditError(
                f"PATCH item {index} must be [positive record number, field, "
                "string new value]",
                filename="PATCH",
            )
        field = triple[1]
        if field not in FIELDS:
            raise AuditError(
                f"PATCH item {index} field must be one of {', '.join(FIELDS)}, "
                f"got {field!r}",
                filename="PATCH",
            )
        normalized.append([triple[0], field, triple[2]])
    return normalized


def _validate_patch_against_finding(
    patch: list[list], identity: tuple, finding: list
) -> None:
    """Validate target scope, uniqueness and per-field new values."""
    if identity[0] == "invalid":
        allowed = {(identity[1], identity[2])}
    else:
        # Stored group finding: [type, [order_id, sku], [record numbers]].
        allowed = {(recno, f) for recno in finding[2] for f in FIELDS}

    targets: set[tuple[int, str]] = set()
    for recno, field, new_value in patch:
        target = (recno, field)
        if target not in allowed:
            if identity[0] == "invalid":
                raise AuditError(
                    f"target [{recno},{field!r}] is not the identity cell "
                    f"[{identity[1]},{identity[2]!r}] of the invalid finding",
                    filename="PATCH",
                )
            raise AuditError(
                f"target record {recno} is not listed by finding {finding[2]} "
                f"for identity {identity[0]!r} {identity[1:]!r}",
                filename="PATCH",
            )
        if target in targets:
            raise AuditError(
                f"PATCH targets cell {target} more than once; targets must be "
                "unique",
                filename="PATCH",
            )
        targets.add(target)
        ok, _ = _field_valid(field, new_value)
        if not ok:
            raise AuditError(
                f"new value {new_value!r} for field {field!r} fails field "
                "validation",
                filename="PATCH",
            )


def run_propose_fix(
    db_path: str,
    batch_id: str,
    identity_text: str,
    patch_text: str,
) -> int:
    """Store a correction proposal for one ``fix`` decision; exit code.

    The identity must carry a ``fix`` decision in ``batch_id`` and the
    complete finding stored with that decision must still match a finding
    of the batch.  PATCH is validated for scope, target uniqueness and
    per-field values.  Repeating the exact same proposal is idempotent
    (exit 0); the same identity with different patch content raises
    :class:`FixConflictError` (exit 3).  Anything else raises
    :class:`AuditError` (exit 2) without writing to the database.
    """
    try:
        identity = json.loads(identity_text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"ID is not valid JSON: {exc}", filename="ID")
    ident_tuple = _validate_identity(identity)
    patch = _parse_patch(patch_text)

    batch = fetch_batch(db_path, batch_id)
    if batch is None:
        raise AuditError(f"batch {batch_id!r} not found", filename=db_path)
    findings = json.loads(batch[2])

    with _fix_transaction(db_path) as conn:
        decisions = _fetch_fix_decisions(conn, batch_id)
        decision = decisions.get(ident_tuple)
        if decision is None:
            ident = _ident_text(ident_tuple)
            if ident_tuple in _fetch_all_decision_identities(conn, batch_id):
                raise AuditError(
                    f"decision for identity {ident} in batch {batch_id!r} is "
                    "not a fix decision",
                    filename="ID",
                )
            raise AuditError(
                f"no fix decision for identity {ident} in batch {batch_id!r}",
                filename="ID",
            )
        _action, reason, decision_finding_json = decision

        matches = [f for f in findings if tuple(finding_identity(f)) == ident_tuple]
        if not matches or _canonical_json(matches[0]) != decision_finding_json:
            raise AuditError(
                f"the stored finding for identity {_ident_text(ident_tuple)} no "
                f"longer matches a finding of batch {batch_id!r}",
                filename="ID",
            )
        finding = matches[0]
        _validate_patch_against_finding(patch, ident_tuple, finding)

        identity_json = _canonical_json(identity)
        finding_json = _canonical_json(finding)
        patch_json = _canonical_json(patch)
        existing = conn.execute(
            "SELECT finding_json, patch_json FROM fix_proposals "
            "WHERE batch_id = ? AND identity_json = ?",
            (batch_id, identity_json),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (finding_json, patch_json):
                raise FixConflictError(
                    f"a fix proposal for identity {identity_json} in batch "
                    f"{batch_id!r} is already stored with different content"
                )
            # Identical proposal: idempotent, the database is not rewritten.
        else:
            conn.execute(
                "INSERT INTO fix_proposals "
                "(batch_id, identity_json, finding_json, patch_json) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, identity_json, finding_json, patch_json),
            )
    return 0


def _decode_csv(data: bytes, input_name: str) -> tuple[str, list[str]]:
    """Decode INPUT (a leading BOM is tolerated) and validate its header."""
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
    if (
        not header
        or any(name == "" for name in header)
        or len(set(header)) != len(header)
    ):
        raise AuditError("malformed CSV header", filename=input_name)
    return text, header


def _normalize_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _apply_patches(
    text: str,
    header_width: int,
    columns: dict[str, int],
    changes: dict[tuple[int, str], str],
    input_name: str,
) -> str:
    """Build the corrected CSV text.

    The CSV is walked exactly as the auditor does (header is record 1,
    skipped blank/whitespace-only records still occupy a record number).
    Untouched logical records are copied verbatim — preserving quoting,
    extra columns and every unchanged cell; only the physical records
    containing a targeted cell are regenerated.  Line endings are
    normalized to ``\\n``.
    """
    physical = text.splitlines(keepends=True)

    def segment(start_line: int, end_line: int) -> str:
        # reader.line_num is 1-based and counts physical lines; the slice
        # translates it to 0-based physical line indices.
        return "".join(physical[start_line - 1:end_line])

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    next(reader)  # header, already validated

    pieces: list[str] = [_normalize_endings(segment(1, 1))]
    record_no = 1
    prev_line = 1
    remaining = dict(changes)
    while True:
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise AuditError(
                f"malformed CSV near record {record_no + 1}: {exc}",
                filename=input_name,
            )
        start_line = prev_line + 1
        end_line = reader.line_num
        record_no += 1
        original = segment(start_line, end_line)
        prev_line = end_line

        if not raw or (len(raw) == 1 and raw[0].strip() == ""):
            pieces.append(_normalize_endings(original))
            continue
        if len(raw) != header_width:
            raise AuditError(
                f"record {record_no} has {len(raw)} fields but the header has "
                f"{header_width}",
                filename=input_name,
            )

        row_changes = {
            field: remaining.pop((record_no, field))
            for field in FIELDS
            if (record_no, field) in remaining
        }
        if not row_changes:
            pieces.append(_normalize_endings(original))
            continue

        row = list(raw)
        for field, new_value in row_changes.items():
            row[columns[field]] = new_value
        buf = io.StringIO(newline="")
        csv.writer(buf, lineterminator="\n").writerow(row)
        emitted = buf.getvalue()
        # Keep a missing trailing terminator on the final physical record.
        if not original.endswith(("\n", "\r")) and emitted.endswith("\n"):
            emitted = emitted[:-1]
        pieces.append(emitted)

    # Physical lines beyond the last record the CSV parser yields (a
    # terminating newline or trailing blank lines) are not records but are
    # still part of the file and must be preserved.
    if prev_line < len(physical):
        pieces.append(_normalize_endings("".join(physical[prev_line:])))

    if remaining:
        target = next(iter(remaining))
        raise AuditError(
            f"patch target record {target[0]} does not exist in the input",
            filename="PATCH",
        )
    return "".join(pieces)


def _build_snapshot(
    decisions: dict[tuple, tuple[str, str, str]],
    proposals: dict[tuple, tuple[str, str]],
) -> list:
    """Identity-sorted traceability snapshot of decisions and proposals.

    Every ``fix`` decision appears with its ``action``, trimmed reason and
    the complete original finding the decision was recorded against;
    proposals additionally carry the complete original finding the proposal
    was stored against and the applied patch.  Stored JSON is echoed back
    verbatim rather than reconstructed from the current batch findings.
    """
    snapshot = []
    for ident in sorted(decisions):
        action, reason, decision_finding_json = decisions[ident]
        entry: dict = {
            "identity": list(ident),
            "action": action,
            "reason": reason,
            "finding": json.loads(decision_finding_json),
        }
        proposal = proposals.get(ident)
        if proposal is not None:
            entry["proposal"] = {
                "finding": json.loads(proposal[0]),
                "patch": json.loads(proposal[1]),
            }
        snapshot.append(entry)
    return snapshot


def _stage_file(path: str, payload: bytes) -> str:
    """Fully write ``payload`` to a temp file next to ``path``; return it."""
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


class _Outputs:
    """Staged file outputs with backups of every target's prior bytes.

    Each replaced target's original bytes (or its prior absence) are
    remembered so a later failure — a second replacement or the database
    commit — can restore both outputs to exactly what the caller saw.
    """

    def __init__(
        self,
        report_payload: bytes,
        report_path: str | None,
        stdout: BinaryIO | TextIO | None,
        csv_path: str,
        csv_payload: bytes,
    ) -> None:
        self.report_path = report_path
        self.csv_path = csv_path
        self._staged: list[tuple[str, str]] = []
        # target -> (original bytes or None when it did not exist, replaced?)
        self._originals: dict[str, tuple[bytes | None, bool]] = {}
        self._report_payload = report_payload
        self._csv_payload = csv_payload
        self._stdout = stdout

    def _backup(self, target: str) -> None:
        if os.path.isdir(target):
            raise AuditError(
                f"cannot write output: {target} is a directory", filename=target
            )
        try:
            with open(target, "rb") as fh:
                original: bytes | None = fh.read()
        except FileNotFoundError:
            original = None
        except OSError as exc:
            raise AuditError(
                f"cannot read existing output: {exc}", filename=target
            )
        # ``replaced`` flips to True only after the replacement lands.
        self._originals[target] = (original, False)

    def _replace(self, target: str, tmp_path: str) -> None:
        try:
            os.replace(tmp_path, target)
        except OSError as exc:
            raise AuditError(f"cannot write output: {exc}", filename=target)
        self._originals[target] = (self._originals[target][0], True)
        self._staged = [(t, p) for t, p in self._staged if p != tmp_path]

    def _cleanup_staged(self) -> None:
        for _target, tmp in self._staged:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        self._staged.clear()

    def emit(self) -> None:
        """Stage both files fully, then replace them one at a time.

        Backups are taken before any replacement so a failure of the
        second replacement can undo the first.
        """
        try:
            self._backup(self.csv_path)
            if self.report_path is not None:
                self._backup(self.report_path)
            csv_tmp = _stage_file(self.csv_path, self._csv_payload)
            self._staged.append((self.csv_path, csv_tmp))
            report_tmp: str | None = None
            if self.report_path is not None:
                report_tmp = _stage_file(self.report_path, self._report_payload)
                self._staged.append((self.report_path, report_tmp))
            else:
                emit_report(self._report_payload, None, self._stdout)
            if report_tmp is not None:
                self._replace(self.report_path, report_tmp)
            self._replace(self.csv_path, csv_tmp)
        except BaseException:
            self._cleanup_staged()
            raise

    def rollback(self) -> list[str]:
        """Restore every replaced target to its prior bytes or absence.

        Returns the descriptions of restore failures (empty on success);
        staged temp files are always cleaned up.
        """
        failures: list[str] = []
        for target, (original, replaced) in self._originals.items():
            if not replaced:
                continue
            try:
                if original is None:
                    # The target did not exist before the call; its current
                    # absence is already the state to restore.
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(target)
                else:
                    fd, tmp_path = tempfile.mkstemp(
                        prefix=f".{os.path.basename(target)}.",
                        suffix=".restore.tmp",
                        dir=os.path.dirname(os.path.abspath(target)) or ".",
                    )
                    try:
                        with os.fdopen(fd, "wb") as fh:
                            fh.write(original)
                            fh.flush()
                            os.fsync(fh.fileno())
                        os.replace(tmp_path, target)
                    except BaseException:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp_path)
                        raise
            except OSError as exc:
                failures.append(f"{target}: {exc}")
        self._cleanup_staged()
        return failures


def run_apply_fixes(
    db_path: str,
    source_id: str,
    derived_id: str,
    input_path: str,
    output_path: str,
    report_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Apply traced fixes, re-audit, and store a derived batch; exit code.

    INPUT's SHA-256 must equal the source batch hash.  Every ``fix``
    decision of the source batch must have a proposal whose stored finding
    still matches.  Proposals are merged ordered by finding identity, then
    record number, then logical field text; distinct findings may target
    the same cell and the value applied last is the one that counts.  The
    corrected CSV is re-audited with the source schema; the audit JSONL
    goes to stdout or ``--report R`` and the corrected CSV is written to
    ``--output``.  A derived batch tracing the new hash, schema, findings,
    source and an identity-sorted decision/proposal snapshot (each
    decision's action, trimmed reason and bound original finding, plus each
    proposal's bound original finding and patch) is stored in one
    transaction together with the file writes.  Repeating the same derived
    id with identical content is idempotent (exit 0); different content
    raises :class:`FixConflictError` (exit 3).  Any stale decision, illegal
    patch, I/O, database or audit error raises :class:`AuditError`
    (exit 2): the transaction is rolled back, both output targets are
    restored to the bytes (or nonexistence) they had before the call, and
    temporary files are cleaned up.  If restoring an output itself fails,
    that is reported alongside the original failure.
    """
    try:
        with open(input_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise AuditError(f"cannot read input file: {exc}", filename=input_path)

    source = fetch_batch(db_path, source_id)
    if source is None:
        raise AuditError(f"batch {source_id!r} not found", filename=db_path)
    source_hash, source_schema_json, source_findings_json = source
    if hashlib.sha256(data).hexdigest() != source_hash:
        raise AuditError(
            f"INPUT hash does not match source batch {source_id!r}",
            filename=input_path,
        )

    schema = json.loads(source_schema_json)
    source_findings = json.loads(source_findings_json)
    text, header = _decode_csv(data, input_path)
    columns = resolve_columns(schema, header)

    # Everything database- or output-related happens in one transaction.
    # Output targets are backed up before replacement and restored if any
    # staging, replacement or commit step fails, so a failed run rolls the
    # derived batch back and leaves both outputs exactly as the caller had
    # them.
    outputs: _Outputs | None = None
    try:
        with _fix_transaction(db_path) as conn:
            decisions = _fetch_fix_decisions(conn, source_id)
            proposals = _fetch_proposals(conn, source_id)

            missing = sorted(set(decisions) - set(proposals))
            if missing:
                raise AuditError(
                    f"fix decision for identity {_ident_text(missing[0])} in batch "
                    f"{source_id!r} has no proposal",
                    filename=db_path,
                )

            # Merge order: finding identity ascending first; within one
            # finding its triples run by record number and then logical
            # field *text* ascending.  Different findings may target the
            # same cell; the later executed value overwrites the earlier
            # one, so that single value governs the CSV, the re-audit and
            # the derived batch.
            ordered_changes: dict[tuple[int, str], str] = {}
            for ident in sorted(proposals):
                if ident not in decisions:
                    raise AuditError(
                        f"stored proposal for identity {_ident_text(ident)} has no "
                        f"fix decision in batch {source_id!r}",
                        filename=db_path,
                    )
                decision_finding_json = decisions[ident][2]
                finding_json, patch_json = proposals[ident]
                matches = [
                    f for f in source_findings
                    if tuple(finding_identity(f)) == ident
                ]
                current_finding_json = (
                    _canonical_json(matches[0]) if matches else None
                )
                if current_finding_json is None:
                    raise AuditError(
                        f"identity {_ident_text(ident)} no longer appears in batch "
                        f"{source_id!r}; the fix decision and proposal are stale",
                        filename=db_path,
                    )
                if current_finding_json != decision_finding_json:
                    raise AuditError(
                        f"fix decision for identity {_ident_text(ident)} no longer "
                        f"matches the finding stored in batch {source_id!r}",
                        filename=db_path,
                    )
                if current_finding_json != finding_json:
                    raise AuditError(
                        f"proposal for identity {_ident_text(ident)} no longer "
                        f"matches a finding of batch {source_id!r}",
                        filename=db_path,
                    )
                finding = matches[0]
                patch = json.loads(patch_json)
                _validate_patch_against_finding(patch, ident, finding)
                for recno, field, new_value in sorted(
                    patch, key=lambda t: (t[0], t[1])
                ):
                    ordered_changes[(recno, field)] = new_value

            corrected_text = _apply_patches(
                text, len(header), columns, ordered_changes, input_path
            )
            corrected_bytes = corrected_text.encode("utf-8")

            findings, _ = audit(
                corrected_bytes, json.dumps(schema), "<corrected csv>"
            )
            payload = serialize(findings)

            snapshot = _build_snapshot(decisions, proposals)
            derived_record = (
                hashlib.sha256(corrected_bytes).hexdigest(),
                _canonical_json(schema),
                _canonical_json(findings[:-1]),
                source_id,
                _canonical_json(snapshot),
            )

            existing = conn.execute(
                "SELECT input_sha256, schema_json, findings_json, source_batch_id, "
                "snapshot_json FROM derived_batches WHERE derived_id = ?",
                (derived_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != derived_record:
                    raise FixConflictError(
                        f"derived batch {derived_id!r} is already stored with "
                        "different content"
                    )
                # Identical derived batch: idempotent; outputs are still emitted.
            else:
                conn.execute(
                    "INSERT INTO derived_batches "
                    "(derived_id, input_sha256, schema_json, findings_json, "
                    "source_batch_id, snapshot_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (derived_id, *derived_record),
                )

            # File outputs are fully staged, then replaced one at a time
            # with the originals backed up; the transaction commits only
            # after both replacements succeed.  The handle exists before
            # emit() runs so a failure between the two replacements can
            # still undo the first.
            outputs = _Outputs(
                payload, report_path, stdout, output_path, corrected_bytes
            )
            outputs.emit()
    except FixConflictError:
        raise
    except AuditError as exc:
        # The transaction has rolled back; restore any output a completed
        # replacement already changed.  A restore failure is reported
        # together with the original failure rather than masked.
        if outputs is not None:
            restore_failures = outputs.rollback()
            if restore_failures:
                exc.message = (
                    f"{exc.message}; additionally, restoring the previous "
                    "output(s) failed: " + "; ".join(restore_failures)
                )
        raise
    return 0
