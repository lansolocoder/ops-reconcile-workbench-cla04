"""``replay-stock`` command: merge adjustments into a trusted report base.

The command reads a JSON report produced by ``adjust-stock`` or
``replay-stock`` as the trusted inventory starting point, plus an
adjustments CSV following the usual contract. Adjustments whose id is
already recorded in the base audit are skipped when every field matches
(any mismatch fails the whole batch); the remaining rows are applied as
one atomic batch and a new JSON report is written.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .adjust_stock import (
    AdjustStockError,
    _atomic_write_report,
    _format_utc,
    _load_adjustments,
    _parse_timestamp,
)

_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _base_text(path: str, where: str, field: str, value: Any) -> str:
    if not isinstance(value, str) or value == "":
        raise AdjustStockError(
            path, None, f"{where} 的 {field} 应为非空字符串: {value!r}"
        )
    return value


def _base_int(
    path: str,
    where: str,
    field: str,
    value: Any,
    *,
    non_negative: bool = False,
    forbid_zero: bool = False,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdjustStockError(
            path, None, f"{where} 的 {field} 应为 JSON 整数: {value!r}"
        )
    if non_negative and value < 0:
        raise AdjustStockError(path, None, f"{where} 的 {field} 不能为负: {value}")
    if forbid_zero and value == 0:
        raise AdjustStockError(path, None, f"{where} 的 {field} 不能为零")
    return value


def _base_timestamp(path: str, where: str, field: str, value: Any) -> datetime:
    if not isinstance(value, str):
        raise AdjustStockError(
            path, None, f"{where} 的 {field} 应为 RFC3339 时间字符串: {value!r}"
        )
    try:
        return _parse_timestamp(path, None, field, value)
    except AdjustStockError as exc:
        raise AdjustStockError(path, None, f"{where}: {exc.reason}") from None


def _load_base(
    path: str,
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    list[str],
]:
    try:
        with open(path, mode="r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise AdjustStockError(path, None, f"无法打开文件: {exc}") from None
    except ValueError as exc:
        raise AdjustStockError(path, None, f"JSON 解析失败: {exc}") from None
    if not isinstance(document, dict):
        raise AdjustStockError(path, None, "BASE 顶层应为 JSON 对象")

    raw_stock = document.get("stock")
    if not isinstance(raw_stock, list):
        raise AdjustStockError(path, None, "BASE 的 stock 应为数组")
    stock: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(raw_stock, start=1):
        where = f"stock 第 {index} 项"
        if not isinstance(item, dict):
            raise AdjustStockError(path, None, f"{where} 应为 JSON 对象")
        warehouse = _base_text(path, where, "warehouse", item.get("warehouse"))
        sku = _base_text(path, where, "sku", item.get("sku"))
        on_hand = _base_int(
            path, where, "on_hand", item.get("on_hand"), non_negative=True
        )
        updated_at = _base_timestamp(path, where, "updated_at", item.get("updated_at"))
        key = (warehouse, sku)
        if key in stock:
            raise AdjustStockError(
                path,
                None,
                f"{where} 业务键 (warehouse={warehouse!r}, sku={sku!r}) 重复",
            )
        stock[key] = {"on_hand": on_hand, "updated_at": updated_at}

    raw_audit = document.get("audit")
    if not isinstance(raw_audit, list):
        raise AdjustStockError(path, None, "BASE 的 audit 应为数组")
    audit: list[dict[str, Any]] = []
    audit_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_audit, start=1):
        where = f"audit 第 {index} 项"
        if not isinstance(item, dict):
            raise AdjustStockError(path, None, f"{where} 应为 JSON 对象")
        entry = {
            "id": _base_text(path, where, "id", item.get("id")),
            "warehouse": _base_text(path, where, "warehouse", item.get("warehouse")),
            "sku": _base_text(path, where, "sku", item.get("sku")),
            "expected": _base_int(
                path, where, "expected", item.get("expected"), non_negative=True
            ),
            "delta": _base_int(
                path, where, "delta", item.get("delta"), forbid_zero=True
            ),
            "reason": _base_text(path, where, "reason", item.get("reason")),
            "occurred_at": _base_timestamp(
                path, where, "occurred_at", item.get("occurred_at")
            ),
            "before": _base_int(
                path, where, "before", item.get("before"), non_negative=True
            ),
            "after": _base_int(
                path, where, "after", item.get("after"), non_negative=True
            ),
        }
        if entry["id"] in audit_by_id:
            raise AdjustStockError(
                path, None, f"{where} 调整 id 重复: {entry['id']!r}"
            )
        audit_by_id[entry["id"]] = entry
        audit.append(entry)

    raw_batches = document.get("batches", [])
    if not isinstance(raw_batches, list):
        raise AdjustStockError(path, None, "BASE 的 batches 应为数组")
    batches: list[str] = []
    for index, item in enumerate(raw_batches, start=1):
        where = f"batches 第 {index} 项"
        if not isinstance(item, str) or _HEX64_RE.fullmatch(item) is None:
            raise AdjustStockError(
                path,
                None,
                f"{where} 应为 64 位小写十六进制字符串: {item!r}",
            )
        if item in batches:
            raise AdjustStockError(path, None, f"{where} 与之前的 batches 值重复")
        batches.append(item)

    return stock, audit, audit_by_id, batches


def _serialize_audit_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "warehouse": entry["warehouse"],
        "sku": entry["sku"],
        "expected": entry["expected"],
        "delta": entry["delta"],
        "reason": entry["reason"],
        "occurred_at": _format_utc(entry["occurred_at"]),
        "before": entry["before"],
        "after": entry["after"],
    }


def _matches_recorded(recorded: dict[str, Any], adjustment: dict[str, Any]) -> bool:
    return (
        recorded["warehouse"] == adjustment["warehouse"]
        and recorded["sku"] == adjustment["sku"]
        and recorded["expected"] == adjustment["expected"]
        and recorded["delta"] == adjustment["delta"]
        and recorded["reason"] == adjustment["reason"]
        and recorded["occurred_at"] == adjustment["occurred_at"]
    )


def run_replay_stock(args: Any) -> int:
    base_path = args.base
    adjustments_path = args.adjustments
    report_path = args.report

    try:
        report_resolved = Path(report_path).resolve()
        for input_path in (base_path, adjustments_path):
            if report_resolved == Path(input_path).resolve():
                raise AdjustStockError(
                    report_path,
                    None,
                    f"REPORT 不得与输入文件同路径: {input_path}",
                )

        stock, audit, audit_by_id, batches = _load_base(base_path)

        try:
            with open(adjustments_path, mode="rb") as handle:
                raw_adjustments = handle.read()
        except OSError as exc:
            raise AdjustStockError(
                adjustments_path, None, f"无法打开文件: {exc}"
            ) from None
        batch_hash = hashlib.sha256(raw_adjustments).hexdigest()

        adjustments = _load_adjustments(adjustments_path)

        # Rows already recorded in the base audit are skipped when every
        # field matches; any mismatch fails the whole batch.
        pending: list[dict[str, Any]] = []
        for adjustment in adjustments:
            recorded = audit_by_id.get(adjustment["id"])
            if recorded is None:
                pending.append(adjustment)
            elif not _matches_recorded(recorded, adjustment):
                raise AdjustStockError(
                    adjustments_path,
                    adjustment["row"],
                    f"调整 id {adjustment['id']!r} 已在 BASE 审计中，"
                    "但字段不一致",
                )

        # Apply by UTC instant, then id ascending.
        pending.sort(key=lambda item: (item["occurred_at"], item["id"]))

        new_entries: list[dict[str, Any]] = []
        for adjustment in pending:
            row = adjustment["row"]
            key = (adjustment["warehouse"], adjustment["sku"])
            state = stock.get(key)
            if state is None:
                raise AdjustStockError(
                    adjustments_path,
                    row,
                    f"调整引用了不存在的库存键 "
                    f"(warehouse={adjustment['warehouse']!r}, "
                    f"sku={adjustment['sku']!r})",
                )
            if adjustment["occurred_at"] < state["updated_at"]:
                raise AdjustStockError(
                    adjustments_path,
                    row,
                    f"occurred_at {_format_utc(adjustment['occurred_at'])} "
                    f"早于该键当前 updated_at "
                    f"{_format_utc(state['updated_at'])}",
                )
            if adjustment["expected"] != state["on_hand"]:
                raise AdjustStockError(
                    adjustments_path,
                    row,
                    f"expected={adjustment['expected']} 与应用前库存 "
                    f"{state['on_hand']} 不一致",
                )
            before = state["on_hand"]
            after = before + adjustment["delta"]
            if after < 0:
                raise AdjustStockError(
                    adjustments_path,
                    row,
                    f"应用后库存为负: {before} + ({adjustment['delta']}) = {after}",
                )

            new_entries.append(
                _serialize_audit_entry(
                    {
                        "id": adjustment["id"],
                        "warehouse": adjustment["warehouse"],
                        "sku": adjustment["sku"],
                        "expected": adjustment["expected"],
                        "delta": adjustment["delta"],
                        "reason": adjustment["reason"],
                        "occurred_at": adjustment["occurred_at"],
                        "before": before,
                        "after": after,
                    }
                )
            )
            state["on_hand"] = after
            state["updated_at"] = adjustment["occurred_at"]

        stock_report = [
            {
                "warehouse": warehouse,
                "sku": sku,
                "on_hand": state["on_hand"],
                "updated_at": _format_utc(state["updated_at"]),
            }
            for (warehouse, sku), state in sorted(stock.items())
        ]
        audit_report = [_serialize_audit_entry(entry) for entry in audit]
        audit_report.extend(new_entries)
        batches_report = list(batches)
        if batch_hash not in batches_report:
            batches_report.append(batch_hash)

        _atomic_write_report(
            report_path,
            {
                "stock": stock_report,
                "audit": audit_report,
                "batches": batches_report,
            },
        )
    except AdjustStockError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    return 0
