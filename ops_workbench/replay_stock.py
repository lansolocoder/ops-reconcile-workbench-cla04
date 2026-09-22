"""``replay-stock`` command: merge adjustments onto a trusted stock report.

The base input is a JSON report previously written by ``adjust-stock`` or
``replay-stock``. Its ``stock`` array is the trusted starting point (stock is
never recomputed from ``audit``); ``audit`` is the history used to detect
adjustments that were already applied; the optional ``batches`` array holds
SHA-256 digests of adjustment files already incorporated.

Adjustments whose id is already in ``audit`` are skipped only when all seven
original input fields match; any mismatch fails the whole batch. Every other
rule violation is likewise fatal and leaves an existing report untouched.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adjust_stock import (
    AdjustStockError,
    _RFC3339_RE,
    _atomic_write_report,
    _format_utc,
    _load_adjustments,
)

STOCK_FIELDS = ["warehouse", "sku", "on_hand", "updated_at"]
AUDIT_FIELDS = [
    "id",
    "warehouse",
    "sku",
    "expected",
    "delta",
    "reason",
    "occurred_at",
    "before",
    "after",
]
_HEX64_PREFIX = "0123456789abcdef"


def _report_text(path: str, where: str, value: Any) -> str:
    if not isinstance(value, str) or value == "":
        raise AdjustStockError(path, None, f"{where} 必须为非空字符串: {value!r}")
    return value


def _report_integer(
    path: str,
    where: str,
    value: Any,
    *,
    non_negative: bool,
    forbid_zero: bool = False,
) -> int:
    # ``bool`` is a subclass of ``int``; JSON booleans are not stock numbers.
    if isinstance(value, bool) or not isinstance(value, int):
        qualifier = "非负" if non_negative else "非零"
        raise AdjustStockError(path, None, f"{where} 必须为{qualifier}JSON 整数: {value!r}")
    if non_negative and value < 0:
        raise AdjustStockError(path, None, f"{where} 不能为负: {value}")
    if forbid_zero and value == 0:
        raise AdjustStockError(path, None, f"{where} 不能为零")
    return value


def _report_timestamp(path: str, where: str, value: Any) -> datetime:
    if not isinstance(value, str) or not _RFC3339_RE.match(value) or value[-1] not in "Zz":
        raise AdjustStockError(
            path, None, f"{where} 必须为 UTC Z 形式的 RFC3339 时间: {value!r}"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise AdjustStockError(path, None, f"{where} 不是有效的日期时间: {value!r}") from None
    return parsed.astimezone(timezone.utc)


def _read_base_json(path: str) -> Any:
    try:
        handle = open(path, mode="rb")
    except OSError as exc:
        raise AdjustStockError(path, None, f"无法打开文件: {exc}") from None
    with handle:
        try:
            raw = handle.read()
        except OSError as exc:
            raise AdjustStockError(path, None, f"无法读取文件: {exc}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AdjustStockError(path, None, f"不是 UTF-8 文件: {exc}") from None
    except json.JSONDecodeError as exc:
        raise AdjustStockError(
            path, None, f"JSON 解析失败: {exc.msg}（位置 {exc.pos}）"
        ) from None


def _load_base_report(path: str) -> dict[str, Any]:
    data = _read_base_json(path)
    if not isinstance(data, dict):
        raise AdjustStockError(path, None, "报告顶层必须为 JSON 对象")

    extra = set(data) - {"stock", "audit", "batches"}
    if extra:
        raise AdjustStockError(
            path, None, f"报告含未知字段: {', '.join(sorted(extra))}"
        )
    if "stock" not in data:
        raise AdjustStockError(path, None, "报告缺少 stock 数组")
    if "audit" not in data:
        raise AdjustStockError(path, None, "报告缺少 audit 数组")
    if not isinstance(data["stock"], list):
        raise AdjustStockError(path, None, "stock 必须为数组")
    if not isinstance(data["audit"], list):
        raise AdjustStockError(path, None, "audit 必须为数组")

    stock: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(data["stock"]):
        where = f"stock 第 {index + 1} 项"
        if not isinstance(item, dict):
            raise AdjustStockError(path, None, f"{where} 必须为对象")
        if set(item) != set(STOCK_FIELDS):
            raise AdjustStockError(
                path,
                None,
                f"{where} 字段应为 {','.join(STOCK_FIELDS)}，实际为 "
                f"{','.join(sorted(item))}",
            )
        warehouse = _report_text(path, f"{where} 的 warehouse", item["warehouse"])
        sku = _report_text(path, f"{where} 的 sku", item["sku"])
        on_hand = _report_integer(path, f"{where} 的 on_hand", item["on_hand"], non_negative=True)
        updated_at = _report_timestamp(path, f"{where} 的 updated_at", item["updated_at"])
        key = (warehouse, sku)
        if key in stock:
            raise AdjustStockError(
                path,
                None,
                f"{where} 业务键 (warehouse={warehouse!r}, sku={sku!r}) 重复",
            )
        stock[key] = {"on_hand": on_hand, "updated_at": updated_at}

    audit_by_id: dict[str, dict[str, Any]] = {}
    audit_rows: list[dict[str, Any]] = []
    for index, item in enumerate(data["audit"]):
        where = f"audit 第 {index + 1} 项"
        if not isinstance(item, dict):
            raise AdjustStockError(path, None, f"{where} 必须为对象")
        if set(item) != set(AUDIT_FIELDS):
            raise AdjustStockError(
                path,
                None,
                f"{where} 字段应为 {','.join(AUDIT_FIELDS)}，实际为 "
                f"{','.join(sorted(item))}",
            )
        adjustment_id = _report_text(path, f"{where} 的 id", item["id"])
        warehouse = _report_text(path, f"{where} 的 warehouse", item["warehouse"])
        sku = _report_text(path, f"{where} 的 sku", item["sku"])
        reason = _report_text(path, f"{where} 的 reason", item["reason"])
        expected = _report_integer(path, f"{where} 的 expected", item["expected"], non_negative=True)
        delta = _report_integer(
            path, f"{where} 的 delta", item["delta"], non_negative=False, forbid_zero=True
        )
        before = _report_integer(path, f"{where} 的 before", item["before"], non_negative=True)
        after = _report_integer(path, f"{where} 的 after", item["after"], non_negative=True)
        occurred_at = _report_timestamp(path, f"{where} 的 occurred_at", item["occurred_at"])
        if adjustment_id in audit_by_id:
            raise AdjustStockError(path, None, f"{where} 调整 id 重复: {adjustment_id!r}")
        row = {
            "id": adjustment_id,
            "warehouse": warehouse,
            "sku": sku,
            "expected": expected,
            "delta": delta,
            "reason": reason,
            "occurred_at": occurred_at,
            "before": before,
            "after": after,
        }
        audit_by_id[adjustment_id] = row
        audit_rows.append(row)

    batches: list[str] = []
    if "batches" in data:
        raw_batches = data["batches"]
        if not isinstance(raw_batches, list):
            raise AdjustStockError(path, None, "batches 必须为数组")
        seen_batches: set[str] = set()
        for index, item in enumerate(raw_batches):
            where = f"batches 第 {index + 1} 项"
            if not isinstance(item, str) or len(item) != 64 or any(
                char not in _HEX64_PREFIX for char in item
            ):
                raise AdjustStockError(
                    path, None, f"{where} 不是 64 位小写十六进制字符串: {item!r}"
                )
            if item in seen_batches:
                raise AdjustStockError(path, None, f"{where} 批次摘要重复: {item}")
            seen_batches.add(item)
            batches.append(item)

    return {
        "stock": stock,
        "audit": audit_rows,
        "audit_by_id": audit_by_id,
        "batches": batches,
    }


def _field_differences(
    adjustment: dict[str, Any], prior: dict[str, Any]
) -> list[str]:
    diffs: list[str] = []
    for field in ("id", "warehouse", "sku", "expected", "delta", "reason"):
        if adjustment[field] != prior[field]:
            diffs.append(field)
    if adjustment["occurred_at"] != prior["occurred_at"]:
        diffs.append("occurred_at")
    return diffs


def _serialize_audit_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "warehouse": row["warehouse"],
        "sku": row["sku"],
        "expected": row["expected"],
        "delta": row["delta"],
        "reason": row["reason"],
        "occurred_at": _format_utc(row["occurred_at"]),
        "before": row["before"],
        "after": row["after"],
    }


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

        base = _load_base_report(base_path)
        adjustments = _load_adjustments(adjustments_path)

        try:
            adjustments_bytes = Path(adjustments_path).read_bytes()
        except OSError as exc:
            raise AdjustStockError(
                adjustments_path, None, f"无法读取文件: {exc}"
            ) from None
        digest = hashlib.sha256(adjustments_bytes).hexdigest()
        batch_already_seen = digest in set(base["batches"])

        # Ids already recorded in audit must match all seven original fields;
        # unknown rows are candidates for application.
        candidates: list[dict[str, Any]] = []
        for adjustment in adjustments:
            prior = base["audit_by_id"].get(adjustment["id"])
            if prior is None:
                candidates.append(adjustment)
                continue
            diffs = _field_differences(adjustment, prior)
            if diffs:
                raise AdjustStockError(
                    adjustments_path,
                    adjustment["row"],
                    f"调整 id {adjustment['id']!r} 已在 BASE.audit 中，"
                    f"但字段不一致: {', '.join(diffs)}",
                )

        stock = base["stock"]
        appended_audit: list[dict[str, Any]] = []
        # A repeated batch changes nothing: no stock updates and no audit rows.
        if not batch_already_seen:
            for adjustment in sorted(
                candidates, key=lambda item: (item["occurred_at"], item["id"])
            ):
                row = adjustment["row"]
                key = (adjustment["warehouse"], adjustment["sku"])
                state = stock.get(key)
                if state is None:
                    raise AdjustStockError(
                        adjustments_path,
                        row,
                        f"调整引用了不存在的业务键 "
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

                appended_audit.append(
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
        audit_report = [_serialize_audit_row(row) for row in base["audit"]]
        audit_report.extend(_serialize_audit_row(row) for row in appended_audit)

        batches_report = list(base["batches"])
        if not batch_already_seen:
            batches_report.append(digest)

        _atomic_write_report(
            report_path,
            {"stock": stock_report, "audit": audit_report, "batches": batches_report},
        )
    except AdjustStockError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    return 0
