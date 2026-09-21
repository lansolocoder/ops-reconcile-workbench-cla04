"""Implementation of the ``audit-orders`` command.

Scans a UTF-8 CSV of orders against a column-mapping schema (a UTF-8 JSON
object) and emits a JSON Lines report.  Only the Python standard library is
used.

Exit codes:
    0 - report generated, no findings
    1 - report generated, one or more findings
    2 - usage/input error (bad schema, encoding, CSV or header); no report
        is written and the reason is reported on stderr
"""

import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime

# Logical field keys in the fixed order used when a row has several errors.
FIELDS = ("order_id", "sku", "qty", "status", "updated_at")

QTY_RE = re.compile(r"[1-9][0-9]*\Z")

# ISO 8601 instant at exactly second precision, with a timezone designator
# (Z, or +/-HH with optional MM, colon optional).  Fractional seconds and
# sub-minute precision are deliberately excluded.
TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}(?::?\d{2})?)\Z"
)

DUPLICATE = "duplicate"
CONFLICT = "conflict"


class AuditError(Exception):
    """An input-level failure that must terminate with exit code 2."""


def _load_schema(schema_arg: str) -> dict[str, list[str]]:
    """Resolve the schema argument and validate its structure.

    The argument may be a path to a JSON file or an inline JSON string.
    """
    text = schema_arg
    source = "schema"
    if os.path.isfile(schema_arg):
        source = schema_arg
        try:
            with open(schema_arg, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise AuditError(f"{schema_arg}: cannot read schema file: {exc}") from exc
        text = _decode_utf8(raw, schema_arg)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuditError(
            f"{source}: schema must be a UTF-8 JSON object ({exc.msg})"
        ) from exc

    if not isinstance(parsed, dict):
        raise AuditError(f"{source}: schema must be a JSON object")
    keys = set(parsed)
    if keys != set(FIELDS):
        missing = ", ".join(f for f in FIELDS if f not in keys)
        unknown = ", ".join(sorted(keys - set(FIELDS)))
        details = []
        if missing:
            details.append(f"missing key(s): {missing}")
        if unknown:
            details.append(f"unexpected key(s): {unknown}")
        raise AuditError(f"{source}: schema must contain exactly the 5 field keys; " + "; ".join(details))

    schema: dict[str, list[str]] = {}
    for field in FIELDS:
        candidates = parsed[field]
        if (
            not isinstance(candidates, list)
            or not candidates
            or not all(isinstance(name, str) and name for name in candidates)
        ):
            raise AuditError(
                f"{source}: schema value for {field!r} must be a non-empty array "
                "of non-empty column-name strings"
            )
        schema[field] = candidates
    return schema


def _decode_utf8(raw: bytes, source: str) -> str:
    """Decode bytes as UTF-8, tolerating a leading byte-order mark."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AuditError(f"{source}: content is not valid UTF-8: {exc}") from exc


def _resolve_columns(
    schema: dict[str, list[str]], headers: list[str], source: str
) -> dict[str, int]:
    """Map each logical field to exactly one, mutually distinct, header column."""
    chosen: dict[str, int] = {}
    for field in FIELDS:
        hits = [index for index, header in enumerate(headers) if header in schema[field]]
        if len(hits) != 1:
            if not hits:
                raise AuditError(
                    f"{source}: field {field!r}: no header matches a candidate column name"
                )
            raise AuditError(
                f"{source}: field {field!r}: candidate names match more than one header column"
            )
        chosen[field] = hits[0]
    if len(set(chosen.values())) != len(chosen):
        raise AuditError(f"{source}: the columns selected for the five fields must be distinct")
    return chosen


def _valid_timestamp(value: str) -> bool:
    """Return True for an ISO 8601 instant with second precision and a timezone."""
    if not TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _audit(data: bytes, schema: dict[str, list[str]], source: str):
    """Run the audit; return ``(findings, data_row_count)``.

    ``findings`` is a list of ``(sort_key, jsonl_value)`` pairs so the caller
    can order them deterministically.  ``source`` labels error messages with
    the input file name.
    """
    text = _decode_utf8(data, source)
    reader = csv.reader(io.StringIO(text))

    try:
        headers = next(reader)
    except StopIteration:
        raise AuditError(f"{source}: CSV is empty (a header row is required)")
    except csv.Error as exc:
        raise AuditError(f"{source}: malformed CSV header: {exc}") from exc

    if not headers:
        raise AuditError(f"{source}: CSV header must not be empty")
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in headers:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise AuditError(
            f"{source}: CSV header contains duplicate column name(s): " + ", ".join(duplicates)
        )

    columns = _resolve_columns(schema, headers, source)

    findings: list[tuple[tuple, object]] = []
    valid_rows: dict[tuple[str, str], list[tuple[int, int, str, str]]] = {}
    data_row_count = 0

    try:
        for record in reader:
            # A blank line is yielded as []: it is not a data row, but it
            # occupies a physical record number (header is record 1), so use
            # the reader's line_num rather than a data-row counter.
            if record == []:
                continue
            data_row_count += 1
            record_no = reader.line_num

            # Rows with fewer/more columns than the header cannot be aligned to
            # fields reliably; treat as malformed CSV.
            if len(record) != len(headers):
                raise AuditError(
                    f"{source}: record {record_no} has {len(record)} fields; "
                    f"expected {len(headers)}"
                )

            raw = {field: record[columns[field]] for field in FIELDS}
            trimmed = {field: raw[field].strip() for field in FIELDS}

            invalid_fields: list[str] = []
            for field in FIELDS:
                value = trimmed[field]
                if field in ("order_id", "sku"):
                    ok = bool(value)
                elif field == "qty":
                    ok = bool(QTY_RE.fullmatch(value))
                elif field == "status":
                    ok = value in ("open", "cancelled")
                else:
                    ok = bool(value) and _valid_timestamp(value)
                if not ok:
                    invalid_fields.append(field)

            if invalid_fields:
                for field in invalid_fields:
                    payload = ["invalid", record_no, field, raw[field]]
                    findings.append(((record_no, "invalid", field), payload))
                continue

            key = (trimmed["order_id"], trimmed["sku"])
            valid_rows.setdefault(key, []).append(
                (
                    record_no,
                    int(trimmed["qty"]),
                    trimmed["status"],
                    trimmed["updated_at"],
                )
            )
    except csv.Error as exc:
        raise AuditError(f"{source}: malformed CSV data: {exc}") from exc

    for (order_id, sku), rows in valid_rows.items():
        if len(rows) < 2:
            continue
        record_numbers = sorted(row[0] for row in rows)
        quantities = {row[1] for row in rows}
        statuses = {row[2] for row in rows}
        timestamps = {row[3] for row in rows}
        if len(quantities) > 1 or len(statuses) > 1 or len(timestamps) > 1:
            findings.append(
                ((record_numbers[0], CONFLICT, ""), [CONFLICT, [order_id, sku], record_numbers])
            )
        findings.append(
            ((record_numbers[0], DUPLICATE, ""), [DUPLICATE, [order_id, sku], record_numbers])
        )

    findings.sort(key=lambda item: item[0])
    return [payload for _, payload in findings], data_row_count


def run(schema_arg: str, input_path: str, output_path: str | None) -> int:
    """Execute the command; returns the process exit code."""
    schema = _load_schema(schema_arg)

    try:
        with open(input_path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise AuditError(f"{input_path}: cannot read input file: {exc}") from exc

    findings, data_row_count = _audit(data, schema, input_path)
    digest = hashlib.sha256(data).hexdigest()

    lines: list[str] = []
    for payload in findings:
        lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    lines.append(
        json.dumps(
            ["summary", data_row_count, len(findings), digest],
            separators=(",", ":"),
        )
    )
    report = "".join(line + "\n" for line in lines)

    if output_path is None:
        # stdout reconfig keeps non-ASCII values intact.
        import sys

        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(report)
    else:
        _atomic_write(output_path, report)

    return 1 if findings else 0


def _atomic_write(output_path: str, report: str) -> None:
    directory = os.path.dirname(os.path.abspath(output_path))
    tmp_path = None
    try:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise AuditError(f"{output_path}: cannot create output directory: {exc}") from exc

        # A unique temporary name in the target directory; no Date/random
        # dependency needed, uniqueness only has to cover our own retries.
        tmp_path = f"{output_path}.tmp"
        counter = 1
        while os.path.exists(tmp_path):
            tmp_path = f"{output_path}.tmp{counter}"
            counter += 1

        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(report)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, output_path)
        tmp_path = None
    except OSError as exc:
        raise AuditError(f"{output_path}: cannot write report: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
