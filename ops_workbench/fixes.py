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

    Every ``fix`` decision appears with its stored ``action``, trimmed
    reason and the complete original finding the decision was bound to
    (taken from the stored decision, never re-derived from the current
    batch); proposals additionally carry the complete original finding and
    the patch they were stored with.
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


class _OutputCommitter:
    """Stage, replace and (on failure) restore the file outputs.

    Before anything is replaced, every target's original bytes and its
    previous (non-)existence are captured.  All payloads are fully staged
    first; only then are the targets replaced in order.  On any failure
    the already-replaced targets are withdrawn again — pre-existing files
    get their exact original bytes back via another atomic replace,
    previously absent targets are removed — and every temporary file of
    this run is cleaned up.  If the recovery itself fails, the raised
    :class:`AuditError` reports both the original failure and the recovery
    failure; success is never claimed.
    """

    def __init__(self, outputs: list[tuple[str, bytes]]):
        self.outputs = outputs
        self.original: dict[str, bytes | None] = {}
        self.existed: dict[str, bool] = {}
        self.staged: dict[str, str] = {}
        self.replaced: list[str] = []

    def prepare(self) -> None:
        """Capture every target's original bytes/existence; preflight dirs."""
        for path, _payload in self.outputs:
            if os.path.isdir(path):
                raise AuditError(
                    f"cannot write output: {path} is a directory",
                    filename=path,
                )
            try:
                with open(path, "rb") as fh:
                    self.original[path] = fh.read()
            except FileNotFoundError:
                self.original[path] = None
                self.existed[path] = False
            except OSError as exc:
                raise AuditError(
                    f"cannot read existing output: {exc}", filename=path
                )
            else:
                self.existed[path] = True

    def stage_all(self) -> None:
        """Fully stage every payload; nothing is replaced at this point."""
        for path, payload in self.outputs:
            self.staged[path] = _stage_file(path, payload)

    def replace_all(self) -> None:
        """Replace the targets in order once every payload is staged.

        Targets already replaced stay recorded in ``self.replaced`` so the
        caller's abort can withdraw them even when a later replace fails.
        """
        try:
            for path, _payload in self.outputs:
                os.replace(self.staged[path], path)
                self.replaced.append(path)
        except OSError as exc:
            raise AuditError(f"cannot write output: {exc}", filename=path)

    def _restore(self) -> list[tuple[str, str]]:
        """Withdraw successful replacements; return per-target restore errors."""
        errors: list[tuple[str, str]] = []
        for path in reversed(self.replaced):
            try:
                if self.existed.get(path):
                    tmp_path = _stage_file(path, self.original[path])
                    try:
                        os.replace(tmp_path, path)
                    except OSError:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp_path)
                        raise
                else:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
            except Exception as exc:  # noqa: BLE001 - every recovery error counts
                errors.append((path, _describe_error(exc)))
        return errors

    def _cleanup_staged(self) -> None:
        for tmp_path in self.staged.values():
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    def abort(self, original: BaseException) -> None:
        """Roll the files back to their captured state and clean temps.

        Returns normally when recovery succeeds (the caller then re-raises
        ``original`` itself); when the recovery itself fails it raises a
        combined :class:`AuditError` describing both the original failure
        and the recovery failure, so success is never claimed.
        """
        restore_errors = self._restore()
        self._cleanup_staged()
        if not restore_errors:
            return
        detail = "; ".join(f"{path}: {text}" for path, text in restore_errors)
        message = (
            f"{_describe_error(original)}; additionally, output recovery "
            "failed and some outputs may not be back to their previous "
            f"state: {detail}"
        )
        raise AuditError(message)


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, AuditError):
        location = f"{exc.filename}: " if exc.filename else ""
        return f"{location}{exc.message}"
    return str(exc) or repr(exc)


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
    (and whose decision's stored finding) still matches.  Changes are
    executed in one global order — finding identity ascending, then record
    number, then the logical field text.  A single proposal may not repeat
    a target, but different findings may modify the same cell; within such
    a cell the later-executed value overrides the earlier one and that
    value is what the corrected CSV, the re-audit and the derived batch
    use.  The audit JSONL goes to stdout or ``--report R``; the corrected
    CSV is written to ``--output``.  A derived batch tracing the new hash,
    schema, findings, source and an identity-sorted decision/proposal
    snapshot (each entry carrying the decision's action, trimmed reason and
    the complete original findings it and its proposal were bound to) is
    stored in one transaction.  Repeating the same derived id with
    identical content is idempotent (exit 0, outputs regenerated);
    different content raises :class:`FixConflictError` (exit 3).

    Before any output is replaced, each target's original bytes and
    previous (non-)existence are captured; any staging, replacement or
    transaction commit failure exits 2, rolls the derived batch back and
    restores both outputs to their pre-call bytes or absence — including
    withdrawing an output that was already replaced.  Temporary files and
    backups are cleaned up.  If the recovery itself fails, the error
    reports both the original and the recovery failure; success is never
    claimed.  INPUT is never modified.
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

    # Everything database-related happens in one transaction; the file
    # outputs are staged and replaced while that transaction is open.  The
    # original bytes (or previous absence) of every output target are
    # captured before any replacement, so any later failure rolls the
    # derived batch back AND withdraws every replaced output.
    committer: _OutputCommitter | None = None
    try:
        with _fix_transaction(db_path) as conn:
            decisions = _fetch_fix_decisions(conn, source_id)
            proposals = _fetch_proposals(conn, source_id)

            missing = sorted(set(decisions) - set(proposals))
            if missing:
                raise AuditError(
                    f"fix decision for identity {_ident_text(missing[0])} in "
                    f"batch {source_id!r} has no proposal",
                    filename=db_path,
                )

            # All changes are executed in one global order: finding
            # identity ascending (element by element), then record number,
            # then the logical field *text*.  A single patch still may not
            # repeat a target, but different findings may modify the same
            # cell; within that cell a later execution overrides an earlier
            # one, so the final CSV, the re-audit and the derived batch all
            # see the last value.
            ordered_changes: dict[tuple[int, str], str] = {}
            for ident in sorted(proposals):
                if ident not in decisions:
                    raise AuditError(
                        f"stored proposal for identity {_ident_text(ident)} has "
                        f"no fix decision in batch {source_id!r}",
                        filename=db_path,
                    )
                _action, _reason, decision_finding_json = decisions[ident]
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
                        f"identity {_ident_text(ident)} no longer appears in "
                        f"batch {source_id!r}; the fix decision and proposal "
                        "are stale",
                        filename=db_path,
                    )
                if current_finding_json != decision_finding_json:
                    raise AuditError(
                        f"fix decision for identity {_ident_text(ident)} no "
                        f"longer matches the finding stored in batch "
                        f"{source_id!r}",
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
                    patch, key=lambda triple: (triple[0], triple[1])
                ):
                    # Last writer wins: another finding may have set this
                    # same cell earlier in the execution order.
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
                "SELECT input_sha256, schema_json, findings_json, "
                "source_batch_id, snapshot_json FROM derived_batches "
                "WHERE derived_id = ?",
                (derived_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != derived_record:
                    raise FixConflictError(
                        f"derived batch {derived_id!r} is already stored with "
                        "different content"
                    )
                # Identical derived batch: idempotent; outputs are still
                # regenerated below.
            else:
                conn.execute(
                    "INSERT INTO derived_batches "
                    "(derived_id, input_sha256, schema_json, findings_json, "
                    "source_batch_id, snapshot_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (derived_id, *derived_record),
                )

            # Capture the previous state of every file target before
            # anything is staged or replaced.
            file_outputs = [(output_path, corrected_bytes)]
            if report_path is not None:
                file_outputs.append((report_path, payload))
            committer = _OutputCommitter(file_outputs)
            committer.prepare()
            committer.stage_all()
            # A stdout report participates in the same failure envelope as
            # the files: a failed write rolls the batch back.
            if report_path is None:
                emit_report(payload, None, stdout)
            # Both files are fully staged; only now are they replaced, in
            # order.  Leaving the with-block commits the transaction.  Any
            # failure is handled below by rollback plus output recovery.
            committer.replace_all()
    except BaseException as exc:
        if committer is not None:
            # Rolls the files back to their captured state; if that recovery
            # itself fails, abort raises a combined error instead of the
            # original, so both failures reach stderr and success is never
            # claimed.
            committer.abort(exc)
        raise
    return 0
