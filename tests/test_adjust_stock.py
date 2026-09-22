"""Checks for the adjust-stock command."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

SNAPSHOT = """\
warehouse,sku,on_hand,updated_at
W1,SKU-A,10,2026-09-01T08:00:00+08:00
W2,SKU-B,3,2026-09-01T00:00:00Z
"""

ADJUSTMENTS = """\
id,warehouse,sku,expected,delta,reason,occurred_at
a2,W1,SKU-A,10,5,restock,2026-09-02T02:00:00+02:00
a1,W1,SKU-A,15,-3,sale,2026-09-02T01:00:00Z
a3,W2,SKU-B,3,-1,damaged,2026-09-03T00:00:00Z
"""


class AdjustStockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.snapshot = self.dir / "snapshot.csv"
        self.adjustments = self.dir / "adjustments.csv"
        self.report = self.dir / "report.json"
        self.snapshot.write_text(SNAPSHOT, encoding="utf-8")
        self.adjustments.write_text(ADJUSTMENTS, encoding="utf-8")

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ops_workbench", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_command(self) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "adjust-stock", str(self.snapshot), str(self.adjustments), str(self.report)
        )

    def test_help_lists_adjust_stock(self) -> None:
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("adjust-stock", result.stdout)

    def test_successful_run(self) -> None:
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(
            report["stock"],
            [
                {
                    "warehouse": "W1",
                    "sku": "SKU-A",
                    "on_hand": 12,
                    "updated_at": "2026-09-02T01:00:00Z",
                },
                {
                    "warehouse": "W2",
                    "sku": "SKU-B",
                    "on_hand": 2,
                    "updated_at": "2026-09-03T00:00:00Z",
                },
            ],
        )
        self.assertEqual(
            report["audit"],
            [
                {
                    "id": "a2",
                    "warehouse": "W1",
                    "sku": "SKU-A",
                    "expected": 10,
                    "delta": 5,
                    "reason": "restock",
                    "occurred_at": "2026-09-02T00:00:00Z",
                    "before": 10,
                    "after": 15,
                },
                {
                    "id": "a1",
                    "warehouse": "W1",
                    "sku": "SKU-A",
                    "expected": 15,
                    "delta": -3,
                    "reason": "sale",
                    "occurred_at": "2026-09-02T01:00:00Z",
                    "before": 15,
                    "after": 12,
                },
                {
                    "id": "a3",
                    "warehouse": "W2",
                    "sku": "SKU-B",
                    "expected": 3,
                    "delta": -1,
                    "reason": "damaged",
                    "occurred_at": "2026-09-03T00:00:00Z",
                    "before": 3,
                    "after": 2,
                },
            ],
        )

    def test_untouched_key_keeps_original_time_in_utc(self) -> None:
        self.adjustments.write_text(
            "id,warehouse,sku,expected,delta,reason,occurred_at\n"
            "a1,W1,SKU-A,10,1,restock,2026-09-02T00:00:00Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["stock"][1]["updated_at"], "2026-09-01T00:00:00Z")

    def test_expected_mismatch_fails_and_keeps_old_report(self) -> None:
        self.report.write_text('{"old": true}\n', encoding="utf-8")
        self.adjustments.write_text(
            "id,warehouse,sku,expected,delta,reason,occurred_at\n"
            "a1,W1,SKU-A,99,1,restock,2026-09-02T00:00:00Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.adjustments), result.stderr)
        self.assertIn("line 2", result.stderr)
        self.assertIn("expected", result.stderr)
        self.assertEqual(self.report.read_text(encoding="utf-8"), '{"old": true}\n')

    def test_unknown_key_fails(self) -> None:
        self.adjustments.write_text(
            "id,warehouse,sku,expected,delta,reason,occurred_at\n"
            "a1,W9,SKU-Z,0,1,restock,2026-09-02T00:00:00Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("line 2", result.stderr)
        self.assertIn("unknown snapshot key", result.stderr)
        self.assertFalse(self.report.exists())

    def test_negative_result_fails(self) -> None:
        self.adjustments.write_text(
            "id,warehouse,sku,expected,delta,reason,occurred_at\n"
            "a1,W2,SKU-B,3,-4,sale,2026-09-02T00:00:00Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("negative", result.stderr)

    def test_adjustment_before_snapshot_time_fails(self) -> None:
        self.adjustments.write_text(
            "id,warehouse,sku,expected,delta,reason,occurred_at\n"
            "a1,W1,SKU-A,10,1,restock,2026-08-31T23:59:59Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("earlier", result.stderr)

    def test_duplicate_snapshot_key_fails(self) -> None:
        self.snapshot.write_text(
            "warehouse,sku,on_hand,updated_at\n"
            "W1,SKU-A,10,2026-09-01T00:00:00Z\n"
            "W1,SKU-A,5,2026-09-01T00:00:00Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.snapshot), result.stderr)
        self.assertIn("line 3", result.stderr)
        self.assertIn("duplicate", result.stderr)

    def test_duplicate_adjustment_id_fails(self) -> None:
        self.adjustments.write_text(
            "id,warehouse,sku,expected,delta,reason,occurred_at\n"
            "a1,W1,SKU-A,10,1,restock,2026-09-02T00:00:00Z\n"
            "a1,W1,SKU-A,11,1,restock,2026-09-03T00:00:00Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("line 3", result.stderr)
        self.assertIn("duplicate adjustment id", result.stderr)

    def test_invalid_snapshot_value_fails(self) -> None:
        self.snapshot.write_text(
            "warehouse,sku,on_hand,updated_at\n"
            "W1,SKU-A,-1,2026-09-01T00:00:00Z\n",
            encoding="utf-8",
        )
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.snapshot), result.stderr)
        self.assertIn("line 2", result.stderr)
        self.assertIn("non-negative", result.stderr)

    def test_bad_header_fails(self) -> None:
        self.snapshot.write_text("warehouse,sku,qty,updated_at\n", encoding="utf-8")
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("header", result.stderr)

    def test_report_path_must_differ_from_inputs(self) -> None:
        result = self.invoke(
            "adjust-stock", str(self.snapshot), str(self.adjustments), str(self.snapshot)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("report path", result.stderr)

    def test_missing_input_file_fails(self) -> None:
        result = self.invoke(
            "adjust-stock",
            str(self.dir / "missing.csv"),
            str(self.adjustments),
            str(self.report),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.csv", result.stderr)


if __name__ == "__main__":
    unittest.main()
