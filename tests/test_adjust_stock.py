"""Tests for the ``adjust-stock`` command."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

SNAPSHOT_HEADER = "warehouse,sku,on_hand,updated_at\n"
ADJUSTMENTS_HEADER = (
    "id,warehouse,sku,expected,delta,reason,occurred_at\n"
)


class AdjustStockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def invoke(self, snapshot: Path, adjustments: Path, report: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ops_workbench",
                "adjust-stock",
                str(snapshot),
                str(adjustments),
                str(report),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def write(self, name: str, content: str) -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path

    def assertFailed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertNotEqual(result.stderr, "")

    def test_happy_path_applies_in_utc_and_id_order(self) -> None:
        snapshot = self.write(
            "snap.csv",
            SNAPSHOT_HEADER
            + "W1, SKU-A ,10,2026-01-01T00:00:00Z\n"
            + "W1,SKU-B,5,2026-01-02T08:30:00+09:00\n"
            + "W2,SKU-A,0,2026-01-01T00:00:00+00:00\n",
        )
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER
            + "a2,W1,SKU-A,15,-3,cycle count,2026-02-01T10:00:00+09:00\n"
            + "a1,W1,SKU-A,10,5,receipt,2026-01-15T12:00:00Z\n"
            + "b1,W1,SKU-B,5,2, found stock ,2026-01-02T00:00:00Z\n"
            + "c1,W2,SKU-A,0,10,transfer in,2026-03-01T00:00:00Z\n",
        )
        report = self.tmp / "report.json"
        result = self.invoke(snapshot, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            data["stock"],
            [
                {
                    "warehouse": "W1",
                    "sku": "SKU-A",
                    "on_hand": 12,
                    "updated_at": "2026-02-01T01:00:00Z",
                },
                {
                    "warehouse": "W1",
                    "sku": "SKU-B",
                    "on_hand": 7,
                    "updated_at": "2026-01-02T00:00:00Z",
                },
                {
                    "warehouse": "W2",
                    "sku": "SKU-A",
                    "on_hand": 10,
                    "updated_at": "2026-03-01T00:00:00Z",
                },
            ],
        )
        self.assertEqual([row["id"] for row in data["audit"]], ["b1", "a1", "a2", "c1"])
        self.assertEqual(data["audit"][0]["reason"], "found stock")
        self.assertEqual(data["audit"][1]["before"], 10)
        self.assertEqual(data["audit"][1]["after"], 15)
        self.assertEqual(data["audit"][2]["before"], 15)
        for number in ("on_hand", "expected", "delta", "before", "after"):
            rows = data["stock"] if number == "on_hand" else data["audit"]
            self.assertIsInstance(rows[0][number], int)

    def test_tie_on_timestamp_orders_by_id(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,100,2026-01-01T00:00:00Z\n")
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER
            + "b,W1,S1,98,2,second,2026-01-05T00:00:00Z\n"
            + "a,W1,S1,108,-10,first,2026-01-05T00:00:00Z\n"
            + "eq,W1,S1,100,8,same,2026-01-01T00:00:00Z\n",
        )
        report = self.tmp / "report.json"
        result = self.invoke(snapshot, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in data["audit"]], ["eq", "a", "b"])
        self.assertEqual(data["stock"][0]["updated_at"], "2026-01-05T00:00:00Z")

    def test_empty_adjustments_keeps_original_timestamp(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,100,2026-01-01T00:00:00Z\n")
        adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER)
        report = self.tmp / "report.json"
        result = self.invoke(snapshot, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["audit"], [])
        self.assertEqual(data["stock"][0]["on_hand"], 100)
        self.assertEqual(data["stock"][0]["updated_at"], "2026-01-01T00:00:00Z")

    def test_occurred_at_before_updated_at_fails_batch(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER + "x1,W1,S1,10,1,late,2025-12-31T23:59:59Z\n",
        )
        report = self.tmp / "report.json"
        result = self.invoke(snapshot, adjustments, report)
        self.assertFailed(result)
        self.assertIn("adj.csv", result.stderr)
        self.assertIn("数据行 1", result.stderr)
        self.assertFalse(report.exists())

    def test_expected_mismatch_fails_batch(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER + "x1,W1,S1,9,1,wrong,2026-02-01T00:00:00Z\n",
        )
        report = self.tmp / "report.json"
        result = self.invoke(snapshot, adjustments, report)
        self.assertFailed(result)
        self.assertIn("expected", result.stderr)

    def test_negative_result_fails_batch(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER + "x1,W1,S1,10,-11,shrink,2026-02-01T00:00:00Z\n",
        )
        result = self.invoke(snapshot, adjustments, self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("负", result.stderr)

    def test_unknown_key_fails_batch(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER + "x1,W9,NOPE,0,1,ghost,2026-02-01T00:00:00Z\n",
        )
        result = self.invoke(snapshot, adjustments, self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("不存在", result.stderr)

    def test_duplicate_id_fails(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER
            + "dup,W1,S1,10,1,a,2026-02-01T00:00:00Z\n"
            + "dup,W1,S1,11,1,b,2026-02-02T00:00:00Z\n",
        )
        result = self.invoke(snapshot, adjustments, self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("数据行 2", result.stderr)

    def test_duplicate_snapshot_key_fails(self) -> None:
        snapshot = self.write(
            "snap.csv",
            SNAPSHOT_HEADER
            + "W1,S1,10,2026-01-01T00:00:00Z\n"
            + "W1, S1 ,11,2026-01-01T00:00:00Z\n",
        )
        adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER)
        result = self.invoke(snapshot, adjustments, self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("重复", result.stderr)

    def test_invalid_field_values_fail(self) -> None:
        adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER)
        cases = [
            ("warehouse,sku,on_hand,updated_at\nW1,,10,2026-01-01T00:00:00Z\n", "sku"),
            ("warehouse,sku,on_hand,updated_at\nW1,S1,-1,2026-01-01T00:00:00Z\n", "on_hand"),
            ("warehouse,sku,on_hand,updated_at\nW1,S1,1.5,2026-01-01T00:00:00Z\n", "on_hand"),
            ("warehouse,sku,on_hand,updated_at\nW1,S1,10,not-a-time\n", "updated_at"),
            ("warehouse,sku,on_hand,updated_at\nW1,S1,10,2026-01-01T00:00:00\n", "RFC3339"),
        ]
        for content, needle in cases:
            with self.subTest(needle=needle):
                snapshot = self.write(f"snap_{needle}.csv", content)
                report = self.tmp / "r.json"
                result = self.invoke(snapshot, adjustments, report)
                self.assertFailed(result)
                self.assertFalse(report.exists())

        snapshot = self.write("ok.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        bad_adjustments = [
            ("x1,W1,S1,10,0,,2026-02-01T00:00:00Z\n", "reason"),
            ("x1,W1,S1,-1,1,r,2026-02-01T00:00:00Z\n", "expected"),
            ("x1,W1,S1,10,1.5,r,2026-02-01T00:00:00Z\n", "delta"),
            ("x1,W1,S1,10,1,r,2026-02-01T00:00:00+25:00\n", "RFC3339"),
        ]
        for line, needle in bad_adjustments:
            with self.subTest(needle=needle):
                adjustments = self.write(f"adj_{needle}.csv", ADJUSTMENTS_HEADER + line)
                result = self.invoke(snapshot, adjustments, self.tmp / "r2.json")
                self.assertFailed(result)

    def test_bad_header_and_wrong_column_count_fail(self) -> None:
        good_snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        good_adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER)

        bad_snapshot_header = self.write(
            "bad.csv", "warehouse,sku,on_hand\nW1,S1,10\n"
        )
        result = self.invoke(bad_snapshot_header, good_adjustments, self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("表头", result.stderr)

        bad_columns = self.write(
            "cols.csv", SNAPSHOT_HEADER + "W1,S1,10\n"
        )
        result = self.invoke(bad_columns, good_adjustments, self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("列数", result.stderr)

    def test_report_same_path_as_input_is_rejected(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER)
        result = self.invoke(snapshot, adjustments, snapshot)
        self.assertFailed(result)
        self.assertIn("同路径", result.stderr)
        # Input file untouched.
        self.assertEqual(
            snapshot.read_text(encoding="utf-8"),
            SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n",
        )

    def test_missing_input_file_fails(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        result = self.invoke(snapshot, self.tmp / "missing.csv", self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("missing.csv", result.stderr)

    def test_failed_batch_leaves_existing_report_untouched(self) -> None:
        snapshot = self.write("snap.csv", SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n")
        good = self.write(
            "good.csv",
            ADJUSTMENTS_HEADER + "x1,W1,S1,10,1,ok,2026-02-01T00:00:00Z\n",
        )
        bad = self.write(
            "bad.csv",
            ADJUSTMENTS_HEADER + "x1,W1,S1,10,-99,bad,2026-02-01T00:00:00Z\n",
        )
        report = self.tmp / "report.json"
        first = self.invoke(snapshot, good, report)
        self.assertEqual(first.returncode, 0, first.stderr)
        original_bytes = report.read_bytes()
        self.assertTrue(original_bytes)

        second = self.invoke(snapshot, bad, report)
        self.assertFailed(second)
        self.assertEqual(report.read_bytes(), original_bytes)

    def test_utf8_bom_and_non_ascii_are_supported(self) -> None:
        snapshot = self.write(
            "snap.csv",
            "\ufeffwarehouse,sku,on_hand,updated_at\n"
            "华东仓,SKU-汉,3,2026-01-01T00:00:00Z\n",
        )
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER
            + "调-1,华东仓,SKU-汉,3,2,盘点盈余,2026-02-01T08:00:00+08:00\n",
        )
        report = self.tmp / "report.json"
        result = self.invoke(snapshot, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["stock"][0]["warehouse"], "华东仓")
        self.assertEqual(data["stock"][0]["on_hand"], 5)
        self.assertEqual(data["stock"][0]["updated_at"], "2026-02-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
