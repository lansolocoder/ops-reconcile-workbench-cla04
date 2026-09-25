"""Implementation of the ``reconcile-fulfillments`` subcommand.

Reconciles an orders CSV against a fulfillments CSV by ``(order_id, sku)``
and reports per-key open/shipped quantities and the reconciliation outcome
as JSON Lines.  Schema, BOM, header, column-mapping, row-width and field
validation follow ``audit-orders``; unlike the audit, any field value error
is fatal here (exit 2, no report).  Only the Python standard library is
used.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import BinaryIO, TextIO

from .orders_audit import (
    FIELDS,
    AuditError,
    _field_valid,
    _open_csv,
    emit_report,
    serialize,
)

# Per-file status vocabularies and the status whose qty is counted.
_ORDERS_STATUSES = ("open", "cancelled")
_FULFILLMENTS_STATUSES = ("shipped", "cancelled")

# All possible outcomes, in summary key order.
OUTCOMES = (
    "cancelled-only",
    "orphan-fulfillment",
    "no-fulfillment",
    "balanced",
    "under",
    "over",
)


def _outcome(open_qty: int, shipped_qty: int) -> str:
    if open_qty == 0 and shipped_qty == 0:
        return "cancelled-only"
    if open_qty == 0:
        return "orphan-fulfillment"
    if shipped_qty == 0:
        return "no-fulfillment"
    if shipped_qty == open_qty:
        return "balanced"
    if shipped_qty < open_qty:
        return "under"
    return "over"


def _load_totals(
    data: bytes,
    schema_text: str,
    input_name: str,
    statuses: tuple[str, ...],
    counted_status: str,
) -> dict[tuple[str, str], int]:
    """Sum the qty of ``counted_status`` rows per ``(order_id, sku)`` key.

    Every valid row contributes its key to the union, even when its status
    is not counted (``cancelled``).  Any invalid field value raises
    :class:`AuditError` (exit 2); no ``invalid`` findings are produced.
    """
    columns, data_rows = _open_csv(data, schema_text, input_name)

    totals: dict[tuple[str, str], int] = defaultdict(int)
    for record_no, raw in data_rows:
        values: dict[str, str] = {}
        for field in FIELDS:
            cell = raw[columns[field]]
            ok, value = _field_valid(field, cell, statuses)
            if not ok:
                raise AuditError(
                    f"record {record_no} has invalid {field}: {cell!r}",
                    filename=input_name,
                )
            values[field] = value
        key = (values["order_id"], values["sku"])
        if values["status"] == counted_status:
            totals[key] += int(values["qty"])
        else:
            totals.setdefault(key, 0)
    return totals


def reconcile(
    orders_data: bytes,
    fulfillments_data: bytes,
    schema_text: str,
    orders_name: str,
    fulfillments_name: str,
) -> list[list]:
    """Run the reconciliation; return the report items including summary."""
    orders = _load_totals(
        orders_data, schema_text, orders_name, _ORDERS_STATUSES, "open"
    )
    fulfillments = _load_totals(
        fulfillments_data,
        schema_text,
        fulfillments_name,
        _FULFILLMENTS_STATUSES,
        "shipped",
    )

    keys = sorted(set(orders) | set(fulfillments))
    counts = {outcome: 0 for outcome in OUTCOMES}
    items: list[list] = []
    for order_id, sku in keys:
        open_qty = orders.get((order_id, sku), 0)
        shipped_qty = fulfillments.get((order_id, sku), 0)
        outcome = _outcome(open_qty, shipped_qty)
        counts[outcome] += 1
        items.append(["reconcile", order_id, sku, open_qty, shipped_qty, outcome])

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
    The report is only emitted after both inputs scan cleanly.
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
        raise AuditError(f"cannot read input file: {exc}", filename=fulfillments_path)

    items = reconcile(
        orders_data, fulfillments_data, schema_text, orders_path, fulfillments_path
    )
    emit_report(serialize(items), output_path, stdout)
    return 0
