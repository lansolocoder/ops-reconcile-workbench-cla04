"""订单与发货对账。

读取订单 CSV 与发货 CSV，输出 JSONL 对账结果。输入不合规时抛出
``ReconcileError``，调用方保证此时不创建、不修改输出文件。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass

ORDERS_HEADER = ["order_id", "line_id", "sku", "ordered_qty", "status"]
FULFILLMENTS_HEADER = ["shipment_id", "order_id", "line_id", "sku", "shipped_qty"]
STATUSES = {"open", "cancelled"}
QTY_PATTERN = re.compile(r"[0-9]+")


class ReconcileError(Exception):
    """输入数据不合规，对账中止。"""


@dataclass
class OrderLine:
    order_id: str
    line_id: str
    sku: str
    qty: int
    status: str
    evidence: list  # [文件名, 行号]


@dataclass
class Fulfillment:
    shipment_id: str
    order_id: str
    line_id: str
    sku: str
    qty: int
    evidence: list  # [文件名, 行号]


def _read_table(path: str, expected_header: list[str]) -> list[tuple[int, list[str]]]:
    """读取 CSV，校验表头，返回 (行号, 行字段) 列表（不含表头）。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                rows = [(reader.line_num, row) for row in reader]
            except UnicodeDecodeError as exc:
                raise ReconcileError(f"{path}: 文件不是有效的 UTF-8: {exc}") from exc
    except OSError as exc:
        raise ReconcileError(f"{path}: 无法读取文件: {exc}") from exc
    if not rows:
        raise ReconcileError(f"{path}: 文件为空，缺少表头行")
    header_line, header = rows[0]
    if header != expected_header:
        raise ReconcileError(
            f"{path}:{header_line}: 表头应为 {','.join(expected_header)}，"
            f"实际为 {','.join(header)!r}"
        )
    return rows[1:]


def _parse_qty(raw: str, path: str, line_no: int, column: str) -> int:
    if not QTY_PATTERN.fullmatch(raw) or int(raw) < 1:
        raise ReconcileError(
            f"{path}:{line_no}: {column} 必须为正整数，实际为 {raw!r}"
        )
    return int(raw)


def _require_non_empty(raw: str, path: str, line_no: int, column: str) -> str:
    if raw == "":
        raise ReconcileError(f"{path}:{line_no}: {column} 不能为空")
    return raw


def _check_width(row: list[str], path: str, line_no: int, width: int) -> None:
    if len(row) != width:
        raise ReconcileError(
            f"{path}:{line_no}: 应为 {width} 列，实际为 {len(row)} 列"
        )


def _load_orders(path: str) -> dict[tuple[str, str], OrderLine]:
    orders: dict[tuple[str, str], OrderLine] = {}
    for line_no, row in _read_table(path, ORDERS_HEADER):
        _check_width(row, path, line_no, len(ORDERS_HEADER))
        order_id, line_id, sku, raw_qty, status = row
        order_id = _require_non_empty(order_id, path, line_no, "order_id")
        line_id = _require_non_empty(line_id, path, line_no, "line_id")
        sku = _require_non_empty(sku, path, line_no, "sku")
        qty = _parse_qty(raw_qty, path, line_no, "ordered_qty")
        if status not in STATUSES:
            raise ReconcileError(
                f"{path}:{line_no}: status 应为 open 或 cancelled，实际为 {status!r}"
            )
        key = (order_id, line_id)
        if key in orders:
            raise ReconcileError(
                f"{path}:{line_no}: 重复的订单键 "
                f"order_id={order_id!r}, line_id={line_id!r}"
            )
        orders[key] = OrderLine(order_id, line_id, sku, qty, status, [path, line_no])
    return orders


def _load_fulfillments(path: str) -> list[Fulfillment]:
    fulfillments: list[Fulfillment] = []
    seen_shipments: set[str] = set()
    for line_no, row in _read_table(path, FULFILLMENTS_HEADER):
        _check_width(row, path, line_no, len(FULFILLMENTS_HEADER))
        shipment_id, order_id, line_id, sku, raw_qty = row
        shipment_id = _require_non_empty(shipment_id, path, line_no, "shipment_id")
        order_id = _require_non_empty(order_id, path, line_no, "order_id")
        line_id = _require_non_empty(line_id, path, line_no, "line_id")
        sku = _require_non_empty(sku, path, line_no, "sku")
        qty = _parse_qty(raw_qty, path, line_no, "shipped_qty")
        if shipment_id in seen_shipments:
            raise ReconcileError(
                f"{path}:{line_no}: 重复的 shipment_id {shipment_id!r}"
            )
        seen_shipments.add(shipment_id)
        fulfillments.append(
            Fulfillment(shipment_id, order_id, line_id, sku, qty, [path, line_no])
        )
    return fulfillments


def _record(
    kind: str,
    order_id: str,
    line_id: str,
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


def build_records(orders_path: str, fulfillments_path: str) -> list[dict]:
    """完成全部校验并生成对账记录；任何不合规都在此阶段抛出异常。"""
    orders = _load_orders(orders_path)
    fulfillments = _load_fulfillments(fulfillments_path)

    by_key: dict[tuple[str, str], list[Fulfillment]] = {}
    for fulfillment in fulfillments:
        key = (fulfillment.order_id, fulfillment.line_id)
        by_key.setdefault(key, []).append(fulfillment)

    records: list[dict] = []
    for key in sorted(orders):
        order = orders[key]
        related = by_key.get(key, [])
        counted = [f for f in related if f.sku == order.sku]
        conflicts = [f for f in related if f.sku != order.sku]
        shipped = sum(f.qty for f in counted)
        difference = order.qty - shipped
        if order.status == "open":
            if difference == 0:
                kind = "matched"
            elif difference > 0:
                kind = "under_shipped"
            else:
                kind = "over_shipped"
        else:
            kind = "matched" if shipped == 0 else "cancelled_but_shipped"
        evidence = [order.evidence] + [f.evidence for f in counted]
        records.append(
            _record(kind, order.order_id, order.line_id, order.qty, shipped,
                    difference, evidence)
        )
        for fulfillment in conflicts:
            records.append(
                _record("sku_conflict", order.order_id, order.line_id, order.qty,
                        fulfillment.qty, None, [order.evidence, fulfillment.evidence])
            )

    orphans = [f for f in fulfillments if (f.order_id, f.line_id) not in orders]
    for fulfillment in sorted(orphans, key=lambda f: f.shipment_id):
        records.append(
            _record("missing_order", fulfillment.order_id, fulfillment.line_id,
                    None, fulfillment.qty, None, [fulfillment.evidence])
        )
    return records


def reconcile(orders_path: str, fulfillments_path: str, output_path: str) -> None:
    """对账并写出 JSONL；输入不合规时输出文件不会被创建或修改。"""
    records = build_records(orders_path, fulfillments_path)
    try:
        with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise ReconcileError(f"{output_path}: 无法写入文件: {exc}") from exc
