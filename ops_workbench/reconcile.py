"""Reconcile order lines against fulfillment shipments.

Input files are UTF-8 comma-separated CSVs. The reconciliation result is a
JSON-Lines report; see ``reconcile_files`` for the record semantics.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from dataclasses import dataclass


ORDER_COLUMNS = ["order_id", "line_id", "sku", "ordered_qty", "status"]
FULFILLMENT_COLUMNS = [
    "shipment_id",
    "order_id",
    "line_id",
    "sku",
    "shipped_qty",
]
VALID_STATUSES = ("open", "cancelled")

FIELD_NAMES = [
    "kind",
    "order_id",
    "line_id",
    "ordered_qty",
    "shipped_qty",
    "difference",
    "evidence",
]

_POSITIVE_INTEGER = re.compile(r"[0-9]+")


class ReconcileError(Exception):
    """Raised when an input file is missing or malformed.

    The message always names the offending file and, where applicable, the
    1-based physical line number.
    """


@dataclass(frozen=True)
class _Order:
    order_id: str
    line_id: str
    sku: str
    ordered_qty: int
    status: str
    line: int


@dataclass(frozen=True)
class _Fulfillment:
    shipment_id: str
    order_id: str
    line_id: str
    sku: str
    shipped_qty: int
    line: int


def _read_csv(path: str, expected_columns: list[str]) -> tuple[str, list[tuple[int, list[str]]]]:
    """Read and structurally validate a CSV file.

    Returns the file name (basename) and ``(line_number, fields)`` data rows.
    Raises :class:`ReconcileError` for any structural problem.
    """
    filename = os.path.basename(path)
    try:
        stream = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise ReconcileError(f"{path}: cannot open file: {detail}") from exc

    try:
        with stream:
            reader = csv.reader(stream)
            try:
                header = next(reader)
            except StopIteration:
                raise ReconcileError(
                    f"{filename}: empty file, expected header: {','.join(expected_columns)}"
                ) from None
            except (csv.Error, UnicodeDecodeError) as exc:
                raise ReconcileError(f"{filename}: cannot read CSV: {exc}") from exc

            if header != expected_columns:
                raise ReconcileError(
                    f"{filename}:1: invalid header, expected: {','.join(expected_columns)}"
                )

            rows: list[tuple[int, list[str]]] = []
            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except (csv.Error, UnicodeDecodeError) as exc:
                    raise ReconcileError(
                        f"{filename}:{reader.line_num}: cannot read CSV: {exc}"
                    ) from exc

                line = reader.line_num
                if not row:
                    raise ReconcileError(f"{filename}:{line}: blank line")
                if len(row) != len(expected_columns):
                    raise ReconcileError(
                        f"{filename}:{line}: expected {len(expected_columns)} columns "
                        f"but found {len(row)}"
                    )
                rows.append((line, row))
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise ReconcileError(f"{filename}: cannot read file: {detail}") from exc

    return filename, rows


def _require_text(value: str, label: str, filename: str, line: int) -> None:
    if value == "":
        raise ReconcileError(f"{filename}:{line}: empty {label}")


def _require_positive_int(value: str, label: str, filename: str, line: int) -> int:
    if not _POSITIVE_INTEGER.fullmatch(value) or int(value) < 1:
        raise ReconcileError(
            f"{filename}:{line}: {label} must be a positive integer, got {value!r}"
        )
    return int(value)


def load_orders(path: str) -> tuple[str, dict[tuple[str, str], _Order]]:
    filename, rows = _read_csv(path, ORDER_COLUMNS)
    orders: dict[tuple[str, str], _Order] = {}
    for line, (order_id, line_id, sku, qty_text, status) in rows:
        _require_text(order_id, "order_id", filename, line)
        _require_text(line_id, "line_id", filename, line)
        _require_text(sku, "sku", filename, line)
        ordered_qty = _require_positive_int(qty_text, "ordered_qty", filename, line)
        if status not in VALID_STATUSES:
            raise ReconcileError(
                f"{filename}:{line}: status must be one of "
                f"{','.join(VALID_STATUSES)}, got {status!r}"
            )
        key = (order_id, line_id)
        if key in orders:
            raise ReconcileError(
                f"{filename}:{line}: duplicate order key "
                f"(order_id={order_id!r}, line_id={line_id!r}), "
                f"first seen on line {orders[key].line}"
            )
        orders[key] = _Order(order_id, line_id, sku, ordered_qty, status, line)
    return filename, orders


def load_fulfillments(path: str) -> tuple[str, list[_Fulfillment]]:
    filename, rows = _read_csv(path, FULFILLMENT_COLUMNS)
    fulfillments: list[_Fulfillment] = []
    shipment_ids: set[str] = set()
    for line, (shipment_id, order_id, line_id, sku, qty_text) in rows:
        _require_text(shipment_id, "shipment_id", filename, line)
        _require_text(order_id, "order_id", filename, line)
        _require_text(line_id, "line_id", filename, line)
        _require_text(sku, "sku", filename, line)
        shipped_qty = _require_positive_int(qty_text, "shipped_qty", filename, line)
        if shipment_id in shipment_ids:
            raise ReconcileError(
                f"{filename}:{line}: duplicate shipment_id {shipment_id!r}"
            )
        shipment_ids.add(shipment_id)
        fulfillments.append(
            _Fulfillment(shipment_id, order_id, line_id, sku, shipped_qty, line)
        )
    return filename, fulfillments


def _record(
    kind: str,
    order_id: str | None,
    line_id: str | None,
    ordered_qty: int | None,
    shipped_qty: int | None,
    difference: int | None,
    evidence: list,
) -> dict:
    return {
        "kind": kind,
        "order_id": order_id,
        "line_id": line_id,
        "ordered_qty": ordered_qty,
        "shipped_qty": shipped_qty,
        "difference": difference,
        "evidence": evidence,
    }


def reconcile_files(orders_path: str, fulfillments_path: str) -> list[dict]:
    """Validate both inputs and return the ordered reconciliation records."""
    orders_filename, orders = load_orders(orders_path)
    fulfillments_filename, fulfillments = load_fulfillments(fulfillments_path)

    linked: dict[tuple[str, str], list[_Fulfillment]] = {key: [] for key in orders}
    orphans: list[_Fulfillment] = []
    for fulfillment in fulfillments:
        key = (fulfillment.order_id, fulfillment.line_id)
        if key in linked:
            linked[key].append(fulfillment)
        else:
            orphans.append(fulfillment)

    records: list[dict] = []
    for key in sorted(orders):
        order = orders[key]
        shipments = linked[key]
        counted = [f for f in shipments if f.sku == order.sku]
        shipped_total = sum(f.shipped_qty for f in counted)
        difference = order.ordered_qty - shipped_total

        if order.status == "open":
            if difference == 0:
                kind = "matched"
            elif difference > 0:
                kind = "under_shipped"
            else:
                kind = "over_shipped"
        elif shipped_total == 0:
            kind = "matched"
        else:
            kind = "cancelled_but_shipped"

        evidence = [[orders_filename, order.line]]
        evidence.extend([fulfillments_filename, f.line] for f in counted)
        records.append(
            _record(
                kind,
                order.order_id,
                order.line_id,
                order.ordered_qty,
                shipped_total,
                difference,
                evidence,
            )
        )

        # Shipments carrying a different SKU never count toward the shipped
        # total; each one is reported separately.
        for fulfillment in shipments:
            if fulfillment.sku != order.sku:
                records.append(
                    _record(
                        "sku_conflict",
                        order.order_id,
                        order.line_id,
                        order.ordered_qty,
                        fulfillment.shipped_qty,
                        None,
                        [
                            [orders_filename, order.line],
                            [fulfillments_filename, fulfillment.line],
                        ],
                    )
                )

    for fulfillment in sorted(orphans, key=lambda f: f.shipment_id):
        records.append(
            _record(
                "missing_order",
                fulfillment.order_id,
                fulfillment.line_id,
                None,
                fulfillment.shipped_qty,
                None,
                [[fulfillments_filename, fulfillment.line]],
            )
        )

    return records


def write_jsonl(path: str, records: list[dict]) -> None:
    """Write records as JSON Lines, replacing ``path`` atomically.

    The output file is only created or replaced after all lines have been
    serialized successfully, so a write failure never leaves a partial report.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(
                    json.dumps(
                        record, ensure_ascii=False, separators=(",", ":")
                    )
                )
                stream.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
