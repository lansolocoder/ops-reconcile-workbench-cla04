"""``adjust-stock`` command: validate and apply stock adjustments.

The command reads a snapshot CSV and an adjustments CSV (both UTF-8),
applies every adjustment as one atomic batch, and writes a JSON report.
Any invalid input or rule violation fails the whole batch and leaves an
existing report untouched.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_HEADER = ["warehouse", "sku", "on_hand", "updated_at"]
ADJUSTMENT_HEADER = [
    "id",
    "warehouse",
    "sku",
    "expected",
    "delta",
    "reason",
    "occurred_at",
]

_INTEGER_RE = re.compile(r"[+-]?[0-9]+$")
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)


class AdjustStockError(Exception):
    """An error tied to an input file (and optionally a data row)."""

    def __init__(self, path: str | os.PathLike[str], row: int | None, reason: str):
        self.path = os.fspath(path)
        self.row = row
        self.reason = reason
        super().__init__(self.render())

    def render(self) -> str:
        location = self.path
        if self.row is not None:
            location = f"{location}: 数据行 {self.row}"
        return f"{location}: {self.reason}"


def _parse_integer(
    path: str,
    row: int,
    field: str,
    value: str,
    *,
    non_negative: bool,
    forbid_zero: bool = False,
) -> int:
    if not _INTEGER_RE.match(value):
        qualifier = "非负" if non_negative else "非零"
        raise AdjustStockError(path, row, f"{field} 不是{qualifier}整数: {value!r}")
    number = int(value)
    if non_negative and number < 0:
        raise AdjustStockError(path, row, f"{field} 不能为负: {number}")
    if forbid_zero and number == 0:
        raise AdjustStockError(path, row, f"{field} 不能为零")
    return number


def _parse_timestamp(path: str, row: int, field: str, value: str) -> datetime:
    if not _RFC3339_RE.match(value):
        raise AdjustStockError(
            path, row, f"{field} 不是含时区的 RFC3339 时间: {value!r}"
        )
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value[-1] in "Zz" else value
        )
    except ValueError:
        raise AdjustStockError(
            path, row, f"{field} 不是有效的日期时间: {value!r}"
        ) from None
    if parsed.tzinfo is None:
        raise AdjustStockError(path, row, f"{field} 缺少时区信息: {value!r}")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    text = value.astimezone(timezone.utc).isoformat()
    if text.endswith("+00:00"):
        text = text[: -len("+00:00")] + "Z"
    return text


def _require_text(value: str, path: str, row: int, field: str) -> str:
    if value == "":
        raise AdjustStockError(path, row, f"{field} 不能为空")
    return value


def _read_table(
    path: str, expected_header: list[str]
) -> list[tuple[int, dict[str, str]]]:
    """Return ``(data_row_number, row_dict)`` pairs, stripped of edge whitespace."""
    try:
        handle = open(path, mode="r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise AdjustStockError(path, None, f"无法打开文件: {exc}") from None

    rows: list[tuple[int, dict[str, str]]] = []
    with handle:
        try:
            reader = csv.reader(handle)
            raw_rows = list(enumerate(reader, start=1))
        except (csv.Error, UnicodeError) as exc:
            raise AdjustStockError(path, None, f"CSV 解析失败: {exc}") from None

    if not raw_rows:
        raise AdjustStockError(path, None, "文件为空，缺少表头")

    header_line, header = raw_rows[0]
    header = [cell.strip() for cell in header]
    if header != expected_header:
        raise AdjustStockError(
            path,
            None,
            f"表头(文件行 {header_line})应为 {','.join(expected_header)}，"
            f"实际为 {','.join(header)}",
        )

    for data_index, (_, raw_row) in enumerate(raw_rows[1:], start=1):
        if len(raw_row) == 0 or (len(raw_row) == 1 and raw_row[0].strip() == ""):
            continue
        if len(raw_row) != len(expected_header):
            raise AdjustStockError(
                path,
                data_index,
                f"列数应为 {len(expected_header)}，实际为 {len(raw_row)}",
            )
        rows.append((data_index, dict(zip(expected_header, (c.strip() for c in raw_row)))))
    return rows


def _load_snapshot(path: str) -> dict[tuple[str, str], dict[str, Any]]:
    stock: dict[tuple[str, str], dict[str, Any]] = {}
    for row, data in _read_table(path, SNAPSHOT_HEADER):
        warehouse = _require_text(data["warehouse"], path, row, "warehouse")
        sku = _require_text(data["sku"], path, row, "sku")
        on_hand = _parse_integer(
            path, row, "on_hand", data["on_hand"], non_negative=True
        )
        updated_at = _parse_timestamp(path, row, "updated_at", data["updated_at"])
        key = (warehouse, sku)
        if key in stock:
            raise AdjustStockError(
                path, row, f"快照键 (warehouse={warehouse!r}, sku={sku!r}) 重复"
            )
        stock[key] = {"on_hand": on_hand, "updated_at": updated_at}
    return stock


def _load_adjustments(path: str) -> list[dict[str, Any]]:
    adjustments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row, data in _read_table(path, ADJUSTMENT_HEADER):
        adjustment_id = _require_text(data["id"], path, row, "id")
        warehouse = _require_text(data["warehouse"], path, row, "warehouse")
        sku = _require_text(data["sku"], path, row, "sku")
        reason = _require_text(data["reason"], path, row, "reason")
        expected = _parse_integer(
            path, row, "expected", data["expected"], non_negative=True
        )
        delta = _parse_integer(
            path, row, "delta", data["delta"], non_negative=False, forbid_zero=True
        )
        occurred_at = _parse_timestamp(
            path, row, "occurred_at", data["occurred_at"]
        )
        if adjustment_id in seen_ids:
            raise AdjustStockError(path, row, f"调整 id 重复: {adjustment_id!r}")
        seen_ids.add(adjustment_id)
        adjustments.append(
            {
                "row": row,
                "id": adjustment_id,
                "warehouse": warehouse,
                "sku": sku,
                "expected": expected,
                "delta": delta,
                "reason": reason,
                "occurred_at": occurred_at,
            }
        )
    return adjustments


def _atomic_write_report(path: str, payload: dict[str, Any]) -> None:
    report_path = Path(path)
    directory = report_path.parent if str(report_path.parent) else Path(".")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{report_path.name}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, report_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise AdjustStockError(path, None, f"无法写入报告: {exc}") from None


def run_adjust_stock(args: Any) -> int:
    snapshot_path = args.snapshot
    adjustments_path = args.adjustments
    report_path = args.report

    try:
        report_resolved = Path(report_path).resolve()
        for input_path in (snapshot_path, adjustments_path):
            if report_resolved == Path(input_path).resolve():
                raise AdjustStockError(
                    report_path,
                    None,
                    f"REPORT 不得与输入文件同路径: {input_path}",
                )

        stock = _load_snapshot(snapshot_path)
        adjustments = _load_adjustments(adjustments_path)

        # Apply by UTC instant, then id ascending.
        adjustments.sort(key=lambda item: (item["occurred_at"], item["id"]))

        audit: list[dict[str, Any]] = []
        for adjustment in adjustments:
            row = adjustment["row"]
            key = (adjustment["warehouse"], adjustment["sku"])
            state = stock.get(key)
            if state is None:
                raise AdjustStockError(
                    adjustments_path,
                    row,
                    f"调整引用了不存在的快照键 "
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

            audit.append(
                {
                    "id": adjustment["id"],
                    "warehouse": adjustment["warehouse"],
                    "sku": adjustment["sku"],
                    "expected": adjustment["expected"],
                    "delta": adjustment["delta"],
                    "reason": adjustment["reason"],
                    "occurred_at": _format_utc(adjustment["occurred_at"]),
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
        _atomic_write_report(
            report_path, {"stock": stock_report, "audit": audit}
        )
    except AdjustStockError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    return 0
