"""Implementation of the ``reconcile-fulfillments`` subcommand.

Reconciles an orders CSV against a fulfillments CSV under one shared
column-mapping schema and reports per-``(order_id, sku)`` quantity
outcomes as JSON Lines.  Schema, BOM, header, column mapping, row width
and the five field validations follow ``audit-orders``; unlike the audit,
any schema, encoding, CSV, header, row-width or field-value problem is
fatal (exit 2, no report).  Only the Python standard library is used.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from typing import BinaryIO, TextIO

from .orders_audit import (
    FIELDS,
    AuditError,
    _field_valid,
    emit_report,
    parse_schema,
    resolve_columns,
    serialize,
)

# Outcome names in their summary-count order.
OUTCOMES = (
    "cancelled-only",
    "orphan-fulfillment",
    "no-fulfillment",
    "balanced",
    "under",
    "over",
)

_ORDERS_STATUSES = ("open", "cancelled")
_FULFILLMENTS_STATUSES = ("shipped", "cancelled")


def _parse_rows(
    data: bytes,
    schema: dict[str, list[str]],
    statuses: tuple[str, ...],
    input_name: str,
) -> list[tuple[str, str, int, str]]:
    """Parse one CSV and return ``(order_id, sku, qty, status)`` per data row.

    Decoding, header, column-mapping, row-width and field-value rules are
    those of the audit; every violation raises :class:`AuditError`
    (exit 2) instead of producing a finding.
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

    rows: list[tuple[str, str, int, str]] = []
    record_no = 1
    while True:
        start_line = reader.line_num + 1
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise AuditError(f"malformed CSV near line {start_line}: {exc}",
                             filename=input_name)
        record_no += 1

        # Blank physical lines and whitespace-only records are skipped,
        # exactly as the audit treats them.
        if not raw or (len(raw) == 1 and raw[0].strip() == ""):
            continue

        if len(raw) != len(header):
            raise AuditError(
                f"record {record_no} has {len(raw)} fields but the header has "
                f"{len(header)}",
                filename=input_name,
            )

        values: dict[str, str] = {}
        for field in FIELDS:
            cell = raw[columns[field]]
            if field == "status":
                ok, value = cell in statuses, cell
            else:
                ok, value = _field_valid(field, cell)
            if not ok:
                allowed = (
                    "|".join(statuses) if field == "status" else None
                )
                detail = (
                    f" (expected {allowed})" if allowed is not None else ""
                )
                raise AuditError(
                    f"record {record_no} has invalid {field} value "
                    f"{cell!r}{detail}",
                    filename=input_name,
                )
            values[field] = value

        rows.append(
            (
                values["order_id"],
                values["sku"],
                int(values["qty"]),
                values["status"],
            )
        )
    return rows


def reconcile(
    orders_data: bytes,
    fulfillments_data: bytes,
    schema_text: str,
    orders_name: str,
    fulfillments_name: str,
) -> list[list]:
    """Reconcile the two inputs; return the report items in output order.

    The union of ``(order_id, sku)`` keys is reported ascending; ``O``
    sums the qty of ORDERS ``open`` rows, ``F`` the qty of FULFILLMENTS
    ``shipped`` rows, and ``cancelled`` rows never count.  The trailing
    ``summary`` item carries the group count, per-outcome counts and the
    SHA-256 of both inputs' raw bytes.  Raises :class:`AuditError` for
    any fatal input problem.
    """
    schema = parse_schema(schema_text)
    orders = _parse_rows(orders_data, schema, _ORDERS_STATUSES, orders_name)
    fulfillments = _parse_rows(
        fulfillments_data, schema, _FULFILLMENTS_STATUSES, fulfillments_name
    )

    open_qty: dict[tuple[str, str], int] = defaultdict(int)
    shipped_qty: dict[tuple[str, str], int] = defaultdict(int)
    keys: set[tuple[str, str]] = set()
    for oid, sku, qty, status in orders:
        key = (oid, sku)
        keys.add(key)
        if status == "open":
            open_qty[key] += qty
    for oid, sku, qty, status in fulfillments:
        key = (oid, sku)
        keys.add(key)
        if status == "shipped":
            shipped_qty[key] += qty

    counts = {outcome: 0 for outcome in OUTCOMES}
    items: list[list] = []
    for oid, sku in sorted(keys):
        o = open_qty[(oid, sku)]
        f = shipped_qty[(oid, sku)]
        if o == 0 and f == 0:
            outcome = "cancelled-only"
        elif o == 0:
            outcome = "orphan-fulfillment"
        elif f == 0:
            outcome = "no-fulfillment"
        elif f == o:
            outcome = "balanced"
        elif f < o:
            outcome = "under"
        else:
            outcome = "over"
        counts[outcome] += 1
        items.append(["reconcile", oid, sku, o, f, outcome])

    items.append(
        [
            "summary",
            len(keys),
            counts,
            hashlib.sha256(orders_data).hexdigest(),
            hashlib.sha256(fulfillments_data).hexdigest(),
        ]
    )
    return items


def run_reconcile(
    schema_text: str,
    orders_path: str,
    fulfillments_path: str,
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """File-level wrapper: read both inputs, emit the report, return 0.

    Fatal problems raise :class:`AuditError`; the caller renders stderr.
    The report is only emitted after both inputs parse and validate
    cleanly, so a failed run produces no report and no ``invalid`` items.
    """
    try:
        with open(orders_path, "rb") as fh:
            orders_data = fh.read()
    except OSError as exc:
        raise AuditError(f"cannot read input file: {exc}", filename=orders_path)
    try:
        with open(fulfillments_path, "rb") as fh:
            fulfillments_data = fh.read()
    except OSError as exc:
        raise AuditError(
            f"cannot read input file: {exc}", filename=fulfillments_path
        )

    items = reconcile(
        orders_data, fulfillments_data, schema_text, orders_path, fulfillments_path
    )
    emit_report(serialize(items), output_path, stdout)
    return 0
