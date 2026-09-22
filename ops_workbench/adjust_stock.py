"""Implementation of the ``adjust-stock`` command.

Reads a stock snapshot CSV and an adjustments CSV, applies the adjustments
in (occurred_at UTC, id) order under strict consistency rules, and writes a
JSON report atomically. Any invalid input or rule conflict aborts the whole
batch with a non-zero exit status and leaves any existing report untouched.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

SNAPSHOT_HEADER = ["warehouse", "sku", "on_hand", "updated_at"]
ADJUSTMENT_HEADER = ["id", "warehouse", "sku", "expected", "delta", "reason", "occurred_at"]

_INTEGER_RE = re.compile(r"[+-]?[0-9]+")
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)


class InputError(Exception):
    """A validation or rule failure tied to an input file and data line."""

    def __init__(self, path: str, line: int | None, reason: str) -> None:
        self.path = path
        self.line = line
        self.reason = reason
        location = f"{path}: line {line}" if line is not None else path
        super().__init__(f"{location}: {reason}")


@dataclass
class SnapshotRow:
    line: int
    warehouse: str
    sku: str
    on_hand: int
    updated_at: datetime


@dataclass
class AdjustmentRow:
    line: int
    id: str
    warehouse: str
    sku: str
    expected: int
    delta: int
    reason: str
    occurred_at: datetime


def run_adjust_stock(snapshot_path: str, adjustments_path: str, report_path: str) -> int:
    """Run the adjust-stock command; returns the process exit status."""
    try:
        _ensure_distinct_paths(snapshot_path, adjustments_path, report_path)
        snapshot = _load_snapshot(snapshot_path)
        adjustments = _load_adjustments(adjustments_path)
        stock, audit = _apply_adjustments(snapshot, adjustments, adjustments_path)
        _write_report(report_path, stock, audit)
    except InputError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _ensure_distinct_paths(snapshot_path: str, adjustments_path: str, report_path: str) -> None:
    report_real = os.path.realpath(report_path)
    for input_path in (snapshot_path, adjustments_path):
        if os.path.realpath(input_path) == report_real:
            raise InputError(
                report_path, None, "report path must differ from the input file paths"
            )


def _read_records(path: str, header: list[str]) -> list[tuple[int, list[str]]]:
    """Read a CSV file, returning (line number, stripped cells) per data row."""
    try:
        handle = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise InputError(path, None, f"cannot read file: {exc.strerror or exc}") from exc
    records: list[tuple[int, list[str]]] = []
    with handle:
        reader = csv.reader(handle)
        header_seen = False
        for record in reader:
            line = reader.line_num
            cells = [cell.strip() for cell in record]
            if not any(cells):
                continue
            if not header_seen:
                header_seen = True
                if cells != header:
                    raise InputError(
                        path, line, f"header must be exactly: {','.join(header)}"
                    )
                continue
            if len(cells) != len(header):
                raise InputError(
                    path, line, f"expected {len(header)} fields, found {len(cells)}"
                )
            records.append((line, cells))
    if not header_seen:
        raise InputError(path, None, "file is empty; expected a header row")
    return records


def _require_non_empty(value: str, field: str, path: str, line: int) -> str:
    if not value:
        raise InputError(path, line, f"{field} must not be empty")
    return value


def _parse_integer(value: str, field: str, path: str, line: int) -> int:
    if not _INTEGER_RE.fullmatch(value):
        raise InputError(path, line, f"{field} must be an integer, got {value!r}")
    return int(value)


def _parse_timestamp(value: str, field: str, path: str, line: int) -> datetime:
    if not _RFC3339_RE.fullmatch(value):
        raise InputError(
            path, line, f"{field} must be an RFC3339 timestamp with timezone, got {value!r}"
        )
    text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InputError(path, line, f"{field} is not a valid timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise InputError(path, line, f"{field} must include a timezone offset")
    return parsed


def _load_snapshot(path: str) -> dict[tuple[str, str], SnapshotRow]:
    rows: dict[tuple[str, str], SnapshotRow] = {}
    for line, cells in _read_records(path, SNAPSHOT_HEADER):
        warehouse, sku, on_hand_text, updated_at_text = cells
        warehouse = _require_non_empty(warehouse, "warehouse", path, line)
        sku = _require_non_empty(sku, "sku", path, line)
        on_hand = _parse_integer(on_hand_text, "on_hand", path, line)
        if on_hand < 0:
            raise InputError(path, line, f"on_hand must be non-negative, got {on_hand}")
        updated_at = _parse_timestamp(updated_at_text, "updated_at", path, line)
        key = (warehouse, sku)
        if key in rows:
            raise InputError(
                path, line, f"duplicate snapshot key (warehouse, sku): {key[0]!r}, {key[1]!r}"
            )
        rows[key] = SnapshotRow(line, warehouse, sku, on_hand, updated_at)
    return rows


def _load_adjustments(path: str) -> list[AdjustmentRow]:
    rows: list[AdjustmentRow] = []
    seen_ids: set[str] = set()
    for line, cells in _read_records(path, ADJUSTMENT_HEADER):
        adj_id, warehouse, sku, expected_text, delta_text, reason, occurred_at_text = cells
        adj_id = _require_non_empty(adj_id, "id", path, line)
        warehouse = _require_non_empty(warehouse, "warehouse", path, line)
        sku = _require_non_empty(sku, "sku", path, line)
        reason = _require_non_empty(reason, "reason", path, line)
        expected = _parse_integer(expected_text, "expected", path, line)
        if expected < 0:
            raise InputError(path, line, f"expected must be non-negative, got {expected}")
        delta = _parse_integer(delta_text, "delta", path, line)
        if delta == 0:
            raise InputError(path, line, "delta must be a non-zero integer")
        occurred_at = _parse_timestamp(occurred_at_text, "occurred_at", path, line)
        if adj_id in seen_ids:
            raise InputError(path, line, f"duplicate adjustment id: {adj_id!r}")
        seen_ids.add(adj_id)
        rows.append(
            AdjustmentRow(line, adj_id, warehouse, sku, expected, delta, reason, occurred_at)
        )
    return rows


def _apply_adjustments(
    snapshot: dict[tuple[str, str], SnapshotRow],
    adjustments: list[AdjustmentRow],
    adjustments_path: str,
) -> tuple[dict[tuple[str, str], list[object]], list[dict[str, object]]]:
    state: dict[tuple[str, str], list[object]] = {
        key: [row.on_hand, row.updated_at] for key, row in snapshot.items()
    }
    for adj in adjustments:
        key = (adj.warehouse, adj.sku)
        if key not in state:
            raise InputError(
                adjustments_path,
                adj.line,
                f"adjustment references unknown snapshot key: {adj.warehouse!r}, {adj.sku!r}",
            )
    audit: list[dict[str, object]] = []
    for adj in sorted(adjustments, key=lambda a: (a.occurred_at, a.id)):
        key = (adj.warehouse, adj.sku)
        on_hand, updated_at = state[key]
        assert isinstance(on_hand, int) and isinstance(updated_at, datetime)
        if adj.occurred_at < updated_at:
            raise InputError(
                adjustments_path,
                adj.line,
                f"occurred_at {_format_utc(adj.occurred_at)} is earlier than the key's "
                f"current updated_at {_format_utc(updated_at)}",
            )
        if adj.expected != on_hand:
            raise InputError(
                adjustments_path,
                adj.line,
                f"expected {adj.expected} does not match current stock {on_hand}",
            )
        after = on_hand + adj.delta
        if after < 0:
            raise InputError(
                adjustments_path,
                adj.line,
                f"applying delta {adj.delta} to stock {on_hand} would make stock negative",
            )
        state[key] = [after, adj.occurred_at]
        audit.append(
            {
                "id": adj.id,
                "warehouse": adj.warehouse,
                "sku": adj.sku,
                "expected": adj.expected,
                "delta": adj.delta,
                "reason": adj.reason,
                "occurred_at": _format_utc(adj.occurred_at),
                "before": on_hand,
                "after": after,
            }
        )
    return state, audit


def _format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_report(
    report_path: str,
    state: dict[tuple[str, str], list[object]],
    audit: list[dict[str, object]],
) -> None:
    stock = [
        {
            "warehouse": warehouse,
            "sku": sku,
            "on_hand": values[0],
            "updated_at": _format_utc(values[1]),
        }
        for (warehouse, sku), values in sorted(state.items())
    ]
    report = {"stock": stock, "audit": audit}
    directory = os.path.dirname(os.path.abspath(report_path))
    fd, temp_path = tempfile.mkstemp(prefix=".adjust-stock-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, report_path)
    except OSError as exc:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise InputError(report_path, None, f"cannot write report: {exc}") from exc
