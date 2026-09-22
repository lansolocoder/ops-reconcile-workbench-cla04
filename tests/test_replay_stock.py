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

SNAPSHOT_HEADER = "warehouse,sku,on_hand,updated_at\n"
ADJUSTMENTS_HEADER = (
    "id,warehouse,sku,expected,delta,reason,occurred_at\n"
)


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

    def invoke_adjust(self, snapshot: Path, adjustments: Path, report: Path) -> subprocess.CompletedProcess[str]:
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

    def base_report(self, stock, audit, batches=None) -> Path:
        payload: dict = {"stock": stock, "audit": audit}
        if batches is not None:
            payload["batches"] = batches
        path = self.tmp / "base.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    def test_applies_new_adjustments_onto_base_without_batches(self) -> None:
        base = self.base_report(
            stock=[
                {"warehouse": "W1", "sku": "S1", "on_hand": 10,
                 "updated_at": "2026-01-01T00:00:00Z"},
                {"warehouse": "W1", "sku": "S2", "on_hand": 5,
                 "updated_at": "2026-01-01T00:00:00Z"},
            ],
            audit=[
                {"id": "old1", "warehouse": "W1", "sku": "S1", "expected": 0,
                 "delta": 10, "reason": "init", "occurred_at": "2026-01-01T00:00:00Z",
                 "before": 0, "after": 10},
            ],
        )
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER
            + "new1,W1,S1,10,3,receipt,2026-02-01T12:00:00+09:00\n"
            + "new0,W1,S2,5,-2,shrink,2026-01-15T00:00:00Z\n",
        )
        report = self.tmp / "out.json"
        result = self.invoke(base, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            data["stock"],
            [
                {"warehouse": "W1", "sku": "S1", "on_hand": 13,
                 "updated_at": "2026-02-01T03:00:00Z"},
                {"warehouse": "W1", "sku": "S2", "on_hand": 3,
                 "updated_at": "2026-01-15T00:00:00Z"},
            ],
        )
        # Old audit kept first, in original order; new rows appended in apply order.
        self.assertEqual([row["id"] for row in data["audit"]], ["old1", "new0", "new1"])
        appended = data["audit"][2]
        self.assertEqual(appended["before"], 10)
        self.assertEqual(appended["after"], 13)
        self.assertEqual(appended["occurred_at"], "2026-02-01T03:00:00Z")

        digest = hashlib.sha256(adjustments.read_bytes()).hexdigest()
        self.assertEqual(data["batches"], [digest])
        self.assertEqual(len(digest), 64)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_runs_after_adjust_stock_and_chains_across_replays(self) -> None:
        snapshot = self.write(
            "snap.csv",
            SNAPSHOT_HEADER + "W1,S1,10,2026-01-01T00:00:00Z\n",
        )
        first_csv = self.write(
            "a1.csv",
            ADJUSTMENTS_HEADER + "a1,W1,S1,10,2,ok,2026-02-01T00:00:00Z\n",
        )
        report1 = self.tmp / "r1.json"
        r = self.invoke_adjust(snapshot, first_csv, report1)
        self.assertEqual(r.returncode, 0, r.stderr)
        first_data = json.loads(report1.read_text(encoding="utf-8"))
        self.assertNotIn("batches", first_data)

        second_csv = self.write(
            "a2.csv",
            ADJUSTMENTS_HEADER + "a2,W1,S1,12,-1,ok,2026-03-01T00:00:00Z\n",
        )
        report2 = self.tmp / "r2.json"
        r = self.invoke(report1, second_csv, report2)
        self.assertEqual(r.returncode, 0, r.stderr)
        data2 = json.loads(report2.read_text(encoding="utf-8"))
        self.assertEqual(data2["stock"][0]["on_hand"], 11)
        self.assertEqual([row["id"] for row in data2["audit"]], ["a1", "a2"])
        d2 = hashlib.sha256(second_csv.read_bytes()).hexdigest()
        self.assertEqual(data2["batches"], [d2])

        third_csv = self.write(
            "a3.csv",
            ADJUSTMENTS_HEADER + "a3,W1,S1,11,4,ok,2026-04-01T00:00:00Z\n",
        )
        report3 = self.tmp / "r3.json"
        r = self.invoke(report2, third_csv, report3)
        self.assertEqual(r.returncode, 0, r.stderr)
        data3 = json.loads(report3.read_text(encoding="utf-8"))
        self.assertEqual(data3["stock"][0]["on_hand"], 15)
        self.assertEqual([row["id"] for row in data3["audit"]], ["a1", "a2", "a3"])
        d3 = hashlib.sha256(third_csv.read_bytes()).hexdigest()
        self.assertEqual(data3["batches"], [d2, d3])

    def test_known_id_with_identical_fields_is_skipped(self) -> None:
        base = self.base_report(
            stock=[{"warehouse": "W1", "sku": "S1", "on_hand": 12,
                    "updated_at": "2026-02-01T00:00:00Z"}],
            audit=[{"id": "a1", "warehouse": "W1", "sku": "S1", "expected": 10,
                    "delta": 2, "reason": "r", "occurred_at": "2026-02-01T00:00:00Z",
                    "before": 10, "after": 12}],
            batches=[],
        )
        # Same logical row expressed with different whitespace/offset
        # normalizes to the same seven fields; plus one genuinely new row.
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER
            + " a1 , W1 , S1 ,10, 2 , r ,2026-02-01T09:00:00+09:00\n"
            + "a2,W1,S1,12,1,next,2026-03-01T00:00:00Z\n",
        )
        report = self.tmp / "out.json"
        result = self.invoke(base, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in data["audit"]], ["a1", "a2"])
        self.assertEqual(data["stock"][0]["on_hand"], 13)

    def test_known_id_with_different_field_fails_batch(self) -> None:
        base = self.base_report(
            stock=[{"warehouse": "W1", "sku": "S1", "on_hand": 12,
                    "updated_at": "2026-02-01T00:00:00Z"}],
            audit=[{"id": "a1", "warehouse": "W1", "sku": "S1", "expected": 10,
                    "delta": 2, "reason": "r", "occurred_at": "2026-02-01T00:00:00Z",
                    "before": 10, "after": 12}],
        )
        cases = [
            "a1,W1,S1,10,3,r,2026-02-01T00:00:00Z\n",       # delta differs
            "a1,W1,S1,11,2,r,2026-02-01T00:00:00Z\n",       # expected differs
            "a1,W1,S1,10,2,r2,2026-02-01T00:00:00Z\n",      # reason differs
            "a1,W2,S1,10,2,r,2026-02-01T00:00:00Z\n",       # warehouse differs
            "a1,W1,S1,10,2,r,2026-02-02T00:00:00Z\n",       # occurred_at differs
        ]
        for line in cases:
            with self.subTest(line=line):
                adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER + line)
                report = self.tmp / "out.json"
                result = self.invoke(base, adjustments, report)
                self.assertFailed(result)
                self.assertIn("adj.csv", result.stderr)
                self.assertIn("数据行 1", result.stderr)
                self.assertIn("不一致", result.stderr)
                self.assertFalse(report.exists())

    def test_repeated_batch_is_a_noop(self) -> None:
        content = (
            ADJUSTMENTS_HEADER
            + "a1,W1,S1,10,2,r,2026-02-01T00:00:00Z\n"
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        base = self.base_report(
            stock=[{"warehouse": "W1", "sku": "S1", "on_hand": 12,
                    "updated_at": "2026-02-01T00:00:00Z"}],
            audit=[{"id": "a1", "warehouse": "W1", "sku": "S1", "expected": 10,
                    "delta": 2, "reason": "r", "occurred_at": "2026-02-01T00:00:00Z",
                    "before": 10, "after": 12}],
            batches=[digest],
        )
        adjustments = self.write("adj.csv", content)
        report = self.tmp / "out.json"
        result = self.invoke(base, adjustments, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["stock"][0]["on_hand"], 12)
        self.assertEqual(len(data["audit"]), 1)
        self.assertEqual(data["batches"], [digest])

    # ------------------------------------------------------------------
    # Application rule failures
    # ------------------------------------------------------------------

    def test_application_rules_fail(self) -> None:
        base = self.base_report(
            stock=[{"warehouse": "W1", "sku": "S1", "on_hand": 10,
                    "updated_at": "2026-02-01T00:00:00Z"}],
            audit=[],
        )
        cases = [
            ("x,W1,S1,10,1,r,2026-01-31T23:59:59Z\n", "updated_at"),
            ("x,W1,S1,9,1,r,2026-02-02T00:00:00Z\n", "expected"),
            ("x,W1,S1,10,-11,r,2026-02-02T00:00:00Z\n", "负"),
            ("x,W9,NOPE,0,1,r,2026-02-02T00:00:00Z\n", "不存在"),
        ]
        for line, needle in cases:
            with self.subTest(needle=needle):
                adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER + line)
                result = self.invoke(base, adjustments, self.tmp / "o.json")
                self.assertFailed(result)
                self.assertIn("数据行 1", result.stderr)
                self.assertIn(needle, result.stderr)

    def test_duplicate_id_in_csv_fails(self) -> None:
        base = self.base_report(
            stock=[{"warehouse": "W1", "sku": "S1", "on_hand": 10,
                    "updated_at": "2026-01-01T00:00:00Z"}],
            audit=[],
        )
        adjustments = self.write(
            "adj.csv",
            ADJUSTMENTS_HEADER
            + "dup,W1,S1,10,1,a,2026-02-01T00:00:00Z\n"
            + "dup,W1,S1,11,1,b,2026-02-02T00:00:00Z\n",
        )
        result = self.invoke(base, adjustments, self.tmp / "o.json")
        self.assertFailed(result)
        self.assertIn("数据行 2", result.stderr)

    def test_failure_leaves_existing_report_untouched(self) -> None:
        base = self.base_report(
            stock=[{"warehouse": "W1", "sku": "S1", "on_hand": 10,
                    "updated_at": "2026-01-01T00:00:00Z"}],
            audit=[],
        )
        good = self.write(
            "good.csv",
            ADJUSTMENTS_HEADER + "g1,W1,S1,10,1,ok,2026-02-01T00:00:00Z\n",
        )
        bad = self.write(
            "bad.csv",
            ADJUSTMENTS_HEADER + "b1,W1,S1,11,-99,no,2026-03-01T00:00:00Z\n",
        )
        report = self.tmp / "report.json"
        first = self.invoke(base, good, report)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = report.read_bytes()

        second = self.invoke(base, bad, report)
        self.assertFailed(second)
        self.assertEqual(report.read_bytes(), original)

    # ------------------------------------------------------------------
    # BASE validation
    # ------------------------------------------------------------------

    def test_invalid_base_reports_fail(self) -> None:
        good_adj = self.write("adj.csv", ADJUSTMENTS_HEADER)

        def write_base(name: str, payload) -> Path:
            path = self.tmp / name
            path.write_text(
                payload if isinstance(payload, str) else json.dumps(payload),
                encoding="utf-8",
            )
            return path

        valid_stock = [{"warehouse": "W1", "sku": "S1", "on_hand": 10,
                        "updated_at": "2026-01-01T00:00:00Z"}]

        cases = [
            ("not-json", "not json at all"),
            ("array.json", []),
            ("missing-stock.json", {"audit": []}),
            ("missing-audit.json", {"stock": valid_stock}),
            ("unknown-field.json", {"stock": valid_stock, "audit": [], "x": 1}),
            ("dup-key.json",
             {"stock": valid_stock + valid_stock, "audit": []}),
            ("dup-audit-id.json",
             {"stock": valid_stock,
              "audit": [
                  {"id": "a", "warehouse": "W1", "sku": "S1", "expected": 10,
                   "delta": 0, "reason": "r",
                   "occurred_at": "2026-01-01T00:00:00Z",
                   "before": 10, "after": 10},
                  {"id": "a", "warehouse": "W1", "sku": "S1", "expected": 10,
                   "delta": 0, "reason": "r",
                   "occurred_at": "2026-01-01T00:00:00Z",
                   "before": 10, "after": 10},
              ]}),
            ("bad-number.json",
             {"stock": [{"warehouse": "W1", "sku": "S1", "on_hand": "10",
                         "updated_at": "2026-01-01T00:00:00Z"}], "audit": []}),
            ("bool-number.json",
             {"stock": [{"warehouse": "W1", "sku": "S1", "on_hand": True,
                         "updated_at": "2026-01-01T00:00:00Z"}], "audit": []}),
            ("negative.json",
             {"stock": [{"warehouse": "W1", "sku": "S1", "on_hand": -1,
                         "updated_at": "2026-01-01T00:00:00Z"}], "audit": []}),
            ("non-utc-time.json",
             {"stock": [{"warehouse": "W1", "sku": "S1", "on_hand": 10,
                         "updated_at": "2026-01-01T00:00:00+00:00"}],
              "audit": []}),
            ("bad-batch.json",
             {"stock": valid_stock, "audit": [], "batches": ["abc"]}),
            ("uppercase-batch.json",
             {"stock": valid_stock, "audit": [],
              "batches": ["A" * 64]}),
            ("dup-batch.json",
             {"stock": valid_stock, "audit": [],
              "batches": ["a" * 64, "a" * 64]}),
        ]
        for name, payload in cases:
            with self.subTest(name=name):
                base = write_base(name, payload)
                report = self.tmp / "out.json"
                result = self.invoke(base, good_adj, report)
                self.assertFailed(result)
                self.assertIn(name, result.stderr)
                self.assertFalse(report.exists())

    def test_missing_base_file_fails(self) -> None:
        adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER)
        result = self.invoke(self.tmp / "nope.json", adjustments, self.tmp / "o.json")
        self.assertFailed(result)
        self.assertIn("nope.json", result.stderr)

    def test_report_same_path_as_input_is_rejected(self) -> None:
        base = self.base_report(
            stock=[{"warehouse": "W1", "sku": "S1", "on_hand": 10,
                    "updated_at": "2026-01-01T00:00:00Z"}],
            audit=[],
        )
        adjustments = self.write("adj.csv", ADJUSTMENTS_HEADER)
        result = self.invoke(base, adjustments, base)
        self.assertFailed(result)
        self.assertIn("同路径", result.stderr)

        result = self.invoke(base, adjustments, adjustments)
        self.assertFailed(result)
        self.assertIn("同路径", result.stderr)


if __name__ == "__main__":
    unittest.main()
