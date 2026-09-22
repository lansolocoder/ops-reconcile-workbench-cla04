"""Implementation of the ``audit-orders`` subcommand.

The audit reads an order CSV and reports field-level validation errors and
cross-row duplicate/conflict findings as JSON Lines.  Only the Python standard
library is used.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, TextIO

# Logical field names; also the validation order within one row.
FIELDS = ("order_id", "sku", "qty", "status", "updated_at")

# Strict ISO 8601 extended format, second precision, with a mandatory time
# zone designator (``Z`` or ``±HH:MM``).  Fractional seconds, minute-only
# offsets, compact/basic formats, space separators and week/ordinal dates
# are intentionally rejected.
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})$"
)
_QTY_RE = re.compile(r"^[1-9][0-9]*$")


class AuditError(Exception):
    """A fatal input error (exit status 2, no report is generated)."""

    def __init__(self, message: str, *, filename: str | None = None):
        super().__init__(message)
        self.message = message
        self.filename = filename


def parse_schema(raw: str) -> dict[str, list[str]]:
    """Validate the schema JSON text and return the candidate-column mapping."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"schema is not valid JSON: {exc}", filename="--schema")
    if not isinstance(obj, dict):
        raise AuditError("schema must be a JSON object", filename="--schema")

    schema: dict[str, list[str]] = {}
    for field in FIELDS:
        if field not in obj:
            raise AuditError(
                f"schema is missing required key {field!r}", filename="--schema"
            )
        candidates = obj[field]
        if not isinstance(candidates, list) or not candidates:
            raise AuditError(
                f"value for {field!r} must be a non-empty array", filename="--schema"
            )
        for cand in candidates:
            if not isinstance(cand, str) or cand == "":
                raise AuditError(
                    f"candidate column names for {field!r} must be non-empty strings",
                    filename="--schema",
                )
        schema[field] = candidates

    extra = sorted(set(obj) - set(FIELDS))
    if extra:
        raise AuditError(
            f"schema contains unsupported keys: {', '.join(extra)}",
            filename="--schema",
        )
    return schema


def resolve_columns(schema: dict[str, list[str]], header: list[str]) -> dict[str, int]:
    """Map each logical field to exactly one header index.

    Each field must match exactly one of its candidate columns and the
    selected columns must be pairwise distinct.
    """
    columns: dict[str, int] = {}
    for field in FIELDS:
        hits = [i for i, name in enumerate(header) if name in schema[field]]
        if not hits:
            raise AuditError(
                f"no header column matches candidates {schema[field]!r} "
                f"for field {field!r}",
                filename="--schema",
            )
        if len(hits) > 1:
            names = sorted(header[i] for i in hits)
            raise AuditError(
                f"multiple header columns match field {field!r}: {', '.join(names)}",
                filename="--schema",
            )
        columns[field] = hits[0]

    chosen: dict[int, str] = {}
    for field in FIELDS:
        idx = columns[field]
        if idx in chosen:
            raise AuditError(
                f"fields {chosen[idx]!r} and {field!r} both select column "
                f"{header[idx]!r}; selected columns must be distinct",
                filename="--schema",
            )
        chosen[idx] = field
    return columns


def _valid_iso8601_seconds(text: str) -> bool:
    match = _ISO_RE.match(text)
    if match is None:
        return False
    year, month, day, hour, minute, second, zone = match.groups()
    if zone == "Z":
        tzinfo = timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        try:
            # timezone() rejects offsets at or beyond ±24:00.
            tzinfo = timezone(
                sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])),
                name=zone,
            )
        except ValueError:
            return False
    try:
        # Calendar validation (e.g. month/day ranges, hour < 24).
        datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=tzinfo,
        )
    except ValueError:
        return False
    return True


def _field_valid(field: str, raw_value: str) -> tuple[bool, str]:
    """Return ``(valid, comparison_value)`` for a raw cell.

    Only ``order_id`` and ``sku`` are trimmed before checking; the other
    fields must match their rule verbatim.
    """
    if field in ("order_id", "sku"):
        value = raw_value.strip()
        return value != "", value
    if field == "qty":
        return _QTY_RE.match(raw_value) is not None, raw_value
    if field == "status":
        return raw_value in ("open", "cancelled"), raw_value
    if field == "updated_at":
        return _valid_iso8601_seconds(raw_value), raw_value
    return False, raw_value  # pragma: no cover - defensive, FIELDS is fixed


def audit(data: bytes, schema_text: str, input_name: str) -> tuple[list[list], int]:
    """Run the audit; return ``(findings, data_row_count)``.

    Findings are plain JSON-compatible lists in their final output order,
    including the trailing ``summary`` item.  Raises :class:`AuditError` for
    fatal schema, encoding, CSV or header errors.
    """
    schema = parse_schema(schema_text)

    # A leading BOM is allowed on INPUT and stripped; the original bytes
    # remain intact for the summary hash.
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

    if not header:
        raise AuditError("CSV header must not be empty", filename=input_name)
    if any(name == "" for name in header):
        raise AuditError(
            "CSV header must not contain an empty column name", filename=input_name
        )
    if len(set(header)) != len(header):
        dupes = sorted({name for name in header if header.count(name) > 1})
        raise AuditError(
            f"duplicate header column names: {', '.join(dupes)}",
            filename=input_name,
        )

    columns = resolve_columns(schema, header)

    findings: list[list] = []
    groups: dict[tuple[str, str], list[tuple[int, int, str, str]]] = defaultdict(list)
    data_row_count = 0
    # Logical record sequence numbers: the header is record 1, and every
    # record the CSV parser yields afterwards increments the count by one.
    # Newlines embedded inside quoted fields do not start a new record, so
    # they do not increment it; blank and whitespace-only records do.
    record_no = 1

    while True:
        # Physical line where the next record begins; only used for the
        # malformed-CSV diagnostic, whose wording stays line-based.
        start_line = reader.line_num + 1
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise AuditError(f"malformed CSV near line {start_line}: {exc}",
                             filename=input_name)
        record_no += 1

        # A blank physical line parses to []; a line containing only
        # whitespace yields a single whitespace cell. Neither is a data row,
        # but both still consume a logical record number.
        if not raw or (len(raw) == 1 and raw[0].strip() == ""):
            continue
        data_row_count += 1

        if len(raw) != len(header):
            raise AuditError(
                f"record {record_no} has {len(raw)} fields but the header has "
                f"{len(header)}",
                filename=input_name,
            )

        values: dict[str, str] = {}
        row_invalid = False
        for field in FIELDS:
            cell = raw[columns[field]]
            ok, value = _field_valid(field, cell)
            values[field] = value
            if not ok:
                row_invalid = True
                findings.append(["invalid", record_no, field, cell])
        if row_invalid:
            continue

        groups[(values["order_id"], values["sku"])].append(
            (
                record_no,
                int(values["qty"]),
                values["status"],
                values["updated_at"],
            )
        )

    for (oid, sku), members in groups.items():
        if len(members) < 2:
            continue
        record_numbers = sorted(m[0] for m in members)
        # Every multi-row group is a duplicate; a group whose qty, status or
        # timestamp text differs additionally reports a conflict.
        findings.append(["duplicate", [oid, sku], record_numbers])
        first = members[0]
        if any(member[1:4] != first[1:4] for member in members[1:]):
            findings.append(["conflict", [oid, sku], record_numbers])

    # Findings sort by the smallest involved record number, then the literal
    # type text ("conflict" < "duplicate" < "invalid" lexicographically, so a
    # group's conflict precedes its duplicate) and finally the field text.
    # Group findings carry no field, so they sort with the empty string; only
    # invalid findings sharing the same record number and type are ordered by
    # the logical field name.
    def sort_key(item: list) -> tuple:
        if item[0] == "invalid":
            return item[1], item[0], item[2]
        return item[2][0], item[0], ""

    findings.sort(key=sort_key)

    digest = hashlib.sha256(data).hexdigest()
    findings.append(["summary", data_row_count, len(findings), digest])
    return findings, data_row_count


def serialize(findings: list[list]) -> bytes:
    lines = [
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in findings
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def run_audit(
    schema_text: str,
    input_path: str,
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """File-level wrapper: read INPUT, produce the report, return exit code.

    Fatal problems raise :class:`AuditError`; the caller renders stderr.
    The report is only emitted after the whole scan succeeds.
    """
    try:
        with open(input_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise AuditError(f"cannot read input file: {exc}", filename=input_path)

    findings, _ = audit(data, schema_text, input_path)
    payload = serialize(findings)
    issue_count = len(findings) - 1  # the last item is the summary

    if output_path is None:
        if stdout is None:  # pragma: no cover - always injected by main
            import sys

            stdout = sys.stdout
        try:
            if hasattr(stdout, "buffer"):
                stdout.buffer.write(payload)
                stdout.buffer.flush()
            else:  # pragma: no cover - binary stream convenience
                stdout.write(payload)
                stdout.flush()
        except OSError as exc:
            raise AuditError(f"cannot write report: {exc}", filename="<stdout>")
    else:
        _atomic_write(output_path, payload)

    return 1 if issue_count else 0


def _atomic_write(path: str, payload: bytes) -> None:
    """Write ``payload`` fully, then atomically replace ``path``.

    On failure any pre-existing ``path`` is left untouched and the
    temporary file is removed.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path: str | None = None
    try:
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
            os.replace(tmp_path, path)
            tmp_path = None
        except OSError as exc:
            raise AuditError(f"cannot write output: {exc}", filename=path)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
