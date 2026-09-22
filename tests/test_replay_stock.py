"""Tests for the ``replay-stock`` command."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

ADJUSTMENTS_HEADER = "id,warehouse,sku,expected,delta,reason,occurred_at\n"

BASE_DOCUMENT = {
    "stock": [
        {
            "warehouse": "W1",
            "sku": "SKU-A",
            "on_hand": 12,
            "updated_at": "2026-02-01T01:00:00Z",
        },
        {
            "warehouse": "W2",
            "sku": "SKU-B",
            "on_hand": 0,
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ],
    "audit": [
        {
            "id": "a1",
            "warehouse": "W1",
            "sku": "SKU-A",
            "expected": 10,
            "delta": 2,
            "reason": "receipt",
            "occurred_at": "2026-02-01T01:00:00Z",
            "before": 10,
            "after": 12,
        }
    ],
}


class ReplayStockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def invoke(self, base: Path, adjustments: Path, report: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ops_workbench",
                "replay-stock",
                str(base),
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

    def write_base(self, document: object = None, name: str = "base.json") -> Path:
        return self.write(name, json.dumps(BASE_DOCUMENT if document is None else document))

    def write_adjustments(self, body: str, name: str = "adj.csv") -> Path:
        return self.write(name, ADJUSTMENTS_HEADER + body)

    def assertFailed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertNotEqual(result.stderr, "")

    def test_happy_path_appends_audit_and_batch_hash(self) -> None:
        base = self.write_base()
        adjustments = self.write_adjustments(
            "n2,W2,SKU-B,0,7,transfer in,2026-03-01T00:00:00Z\n"
            "n1,W1,SKU-A,12,-2,cycle count,2026-02-10T08:00:00+08:00\n"
        )
        report = self.tmp / "report.json"
        result = self.invoke(base, adjustments, report)
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
                    "on_hand": 10,
                    "updated_at": "2026-02-10T00:00:00Z",
                },
                {
                    "warehouse": "W2",
                    "sku": "SKU-B",
                    "on_hand": 7,
                    "updated_at": "2026-03-01T00:00:00Z",
                },
            ],
        )
        # Base audit order preserved, new entries appended in apply order.
        self.assertEqual([row["id"] for row in data["audit"]], ["a1", "n1", "n2"])
        self.assertEqual(data["audit"][1]["before"], 12)
        self.assertEqual(data["audit"][1]["after"], 10)
        self.assertEqual(data["audit"][1]["occurred_at"], "2026-02-10T00:00:00Z")
        for row in data["audit"]:
            for field in ("expected", "delta", "before", "after"):
                self.assertIsInstance(row[field], int)

        expected_hash = hashlib.sha256(adjustments.read_bytes()).hexdigest()
        self.assertEqual(data["batches"], [expected_hash])

    def test_matching_recorded_id_is_skipped(self) -> None:
        base = self.write_base()
        # Same seven fields as the recorded a1 entry, occurred_at in +09:00.
        adjustments = self.write_adjustments(
            "a1,W1,SKU-A,10,2,receipt,2026-02-01T10:00:00+09:00\n"
            "n1,W1,SKU-A,12,3,restock,2026-02-05T00:00:00Z\n"
        )
        report = self.tmp / "report.json"
        result = self.invoke(base, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in data["audit"]], ["a1", "n1"])
        self.assertEqual(data["stock"][0]["on_hand"], 15)

    def test_replaying_same_batch_changes_nothing(self) -> None:
        base = self.write_base()
        adjustments = self.write_adjustments(
            "n1,W1,SKU-A,12,3,restock,2026-02-05T00:00:00Z\n"
        )
        first_report = self.tmp / "first.json"
        first = self.invoke(base, adjustments, first_report)
        self.assertEqual(first.returncode, 0, first.stderr)

        second_report = self.tmp / "second.json"
        second = self.invoke(first_report, adjustments, second_report)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            json.loads(first_report.read_text(encoding="utf-8")),
            json.loads(second_report.read_text(encoding="utf-8")),
        )
        data = json.loads(second_report.read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in data["audit"]], ["a1", "n1"])
        self.assertEqual(len(data["batches"]), 1)

    def test_conflicting_recorded_id_fails_batch(self) -> None:
        base = self.write_base()
        adjustments = self.write_adjustments(
            "a1,W1,SKU-A,10,3,receipt,2026-02-01T01:00:00Z\n"
        )
        report = self.tmp / "report.json"
        result = self.invoke(base, adjustments, report)
        self.assertFailed(result)
        self.assertIn("adj.csv", result.stderr)
        self.assertIn("数据行 1", result.stderr)
        self.assertIn("a1", result.stderr)
        self.assertFalse(report.exists())

    def test_base_batches_are_preserved_and_extended(self) -> None:
        existing = "a" * 64
        document = dict(BASE_DOCUMENT, batches=[existing])
        base = self.write_base(document)
        adjustments = self.write_adjustments("")
        report = self.tmp / "report.json"
        result = self.invoke(base, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        expected_hash = hashlib.sha256(adjustments.read_bytes()).hexdigest()
        self.assertEqual(data["batches"], [existing, expected_hash])

    def test_application_rules_still_enforced(self) -> None:
        cases = [
            ("x1,W1,SKU-A,11,1,wrong expected,2026-02-05T00:00:00Z\n", "expected"),
            ("x1,W9,NOPE,0,1,ghost,2026-02-05T00:00:00Z\n", "不存在"),
            ("x1,W1,SKU-A,12,1,too early,2026-01-15T00:00:00Z\n", "早于"),
            ("x1,W1,SKU-A,12,-99,shrink,2026-02-05T00:00:00Z\n", "负"),
        ]
        for body, needle in cases:
            with self.subTest(needle=needle):
                base = self.write_base(name=f"base_{needle}.json")
                adjustments = self.write_adjustments(body, name=f"adj_{needle}.csv")
                report = self.tmp / f"report_{needle}.json"
                result = self.invoke(base, adjustments, report)
                self.assertFailed(result)
                self.assertIn("数据行 1", result.stderr)
                self.assertIn(needle, result.stderr)
                self.assertFalse(report.exists())

    def test_invalid_base_documents_fail(self) -> None:
        def stock_item(**overrides: object) -> dict[str, object]:
            item = dict(BASE_DOCUMENT["stock"][0])
            item.update(overrides)
            return item

        def audit_item(**overrides: object) -> dict[str, object]:
            item = dict(BASE_DOCUMENT["audit"][0])
            item.update(overrides)
            return item

        cases = [
            ("not an object", "顶层"),
            ({"audit": []}, "stock"),
            ({"stock": []}, "audit"),
            (
                {"stock": [stock_item(), stock_item()], "audit": []},
                "重复",
            ),
            (
                {
                    "stock": [stock_item()],
                    "audit": [audit_item(), audit_item(id="a1")],
                },
                "id 重复",
            ),
            (
                {"stock": [stock_item(on_hand=1.5)], "audit": []},
                "整数",
            ),
            (
                {"stock": [stock_item(on_hand=True)], "audit": []},
                "整数",
            ),
            (
                {"stock": [stock_item(updated_at="not-a-time")], "audit": []},
                "updated_at",
            ),
            (
                {"stock": [stock_item()], "audit": [], "batches": ["XYZ"]},
                "十六进制",
            ),
            (
                {"stock": [stock_item()], "audit": [], "batches": ["a" * 64, "a" * 64]},
                "重复",
            ),
            (
                {"stock": [stock_item()], "audit": [], "batches": "a" * 64},
                "数组",
            ),
        ]
        adjustments = self.write_adjustments("")
        for document, needle in cases:
            with self.subTest(needle=needle):
                base = self.write("bad_base.json", json.dumps(document))
                report = self.tmp / "report.json"
                result = self.invoke(base, adjustments, report)
                self.assertFailed(result)
                self.assertIn("bad_base.json", result.stderr)
                self.assertIn(needle, result.stderr)
                self.assertFalse(report.exists())

    def test_report_same_path_as_input_is_rejected(self) -> None:
        base = self.write_base()
        adjustments = self.write_adjustments("")
        for report in (base, adjustments):
            with self.subTest(report=report):
                result = self.invoke(base, adjustments, report)
                self.assertFailed(result)
                self.assertIn("同路径", result.stderr)

    def test_missing_input_file_fails(self) -> None:
        base = self.write_base()
        adjustments = self.write_adjustments("")
        result = self.invoke(self.tmp / "missing.json", adjustments, self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("missing.json", result.stderr)
        result = self.invoke(base, self.tmp / "missing.csv", self.tmp / "r.json")
        self.assertFailed(result)
        self.assertIn("missing.csv", result.stderr)

    def test_failed_replay_leaves_existing_report_untouched(self) -> None:
        base = self.write_base()
        good = self.write_adjustments(
            "n1,W1,SKU-A,12,1,ok,2026-02-05T00:00:00Z\n", name="good.csv"
        )
        bad = self.write_adjustments(
            "n1,W1,SKU-A,12,-99,bad,2026-02-05T00:00:00Z\n", name="bad.csv"
        )
        report = self.tmp / "report.json"
        first = self.invoke(base, good, report)
        self.assertEqual(first.returncode, 0, first.stderr)
        original_bytes = report.read_bytes()

        second = self.invoke(base, bad, report)
        self.assertFailed(second)
        self.assertEqual(report.read_bytes(), original_bytes)

    def test_base_from_adjust_stock_is_accepted(self) -> None:
        snapshot = self.write(
            "snap.csv",
            "warehouse,sku,on_hand,updated_at\nW1,S1,10,2026-01-01T00:00:00Z\n",
        )
        seed_adjustments = self.write(
            "seed.csv",
            ADJUSTMENTS_HEADER + "s1,W1,S1,10,5,receipt,2026-01-15T00:00:00Z\n",
        )
        base = self.tmp / "base.json"
        seed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ops_workbench",
                "adjust-stock",
                str(snapshot),
                str(seed_adjustments),
                str(base),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(seed.returncode, 0, seed.stderr)

        adjustments = self.write_adjustments(
            "s1,W1,S1,10,5,receipt,2026-01-15T00:00:00Z\n"
            "n1,W1,S1,15,-2,shrink,2026-02-01T00:00:00Z\n"
        )
        report = self.tmp / "report.json"
        result = self.invoke(base, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in data["audit"]], ["s1", "n1"])
        self.assertEqual(data["stock"][0]["on_hand"], 13)
        self.assertEqual(len(data["batches"]), 1)

    def test_help_lists_replay_stock(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "ops_workbench", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("replay-stock", result.stdout)
        self.assertIn("adjust-stock", result.stdout)


if __name__ == "__main__":
    unittest.main()
