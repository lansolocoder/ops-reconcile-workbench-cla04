"""Checks for the reconcile subcommand."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

ORDERS_HEADER = "order_id,line_id,sku,ordered_qty,status\n"
FULFILLMENTS_HEADER = "shipment_id,order_id,line_id,sku,shipped_qty\n"


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.orders = self.dir / "orders.csv"
        self.fulfillments = self.dir / "fulfillments.csv"
        self.output = self.dir / "report.jsonl"

    def write_inputs(self, orders: str, fulfillments: str) -> None:
        self.orders.write_text(ORDERS_HEADER + orders, encoding="utf-8")
        self.fulfillments.write_text(
            FULFILLMENTS_HEADER + fulfillments, encoding="utf-8"
        )

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ops_workbench", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_reconcile(self) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "reconcile",
            "--orders", str(self.orders),
            "--fulfillments", str(self.fulfillments),
            "--output", str(self.output),
        )

    def read_report(self) -> list[dict]:
        text = self.output.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_reconcile_all_kinds(self) -> None:
        self.write_inputs(
            "o1,1,sku-a,10,open\n"
            "o2,1,sku-b,10,open\n"
            "o3,1,sku-c,5,open\n"
            "o4,1,sku-d,7,cancelled\n"
            "o5,1,sku-e,3,cancelled\n"
            "o6,1,sku-f,4,open\n",
            "s1,o1,1,sku-a,10\n"
            "s2,o2,1,sku-b,4\n"
            "s3,o3,1,sku-c,6\n"
            "s4,o4,1,sku-d,2\n"
            "s5,o6,1,sku-x,1\n"
            "s6,o9,1,sku-a,5\n",
        )
        result = self.run_reconcile()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        records = self.read_report()
        self.assertEqual(
            [r["kind"] for r in records],
            [
                "matched",            # o1: 10 - 10
                "under_shipped",      # o2: 10 - 4
                "over_shipped",       # o3: 5 - 6
                "cancelled_but_shipped",  # o4: cancelled, shipped 2
                "matched",            # o5: cancelled, shipped 0
                "under_shipped",      # o6: conflict not counted in S
                "sku_conflict",       # o6 vs sku-x
                "missing_order",      # s6 has no order
            ],
        )
        self.assertEqual(
            [(r["order_id"], r["ordered_qty"], r["shipped_qty"], r["difference"])
             for r in records],
            [
                ("o1", 10, 10, 0),
                ("o2", 10, 4, 6),
                ("o3", 5, 6, -1),
                ("o4", 7, 2, 5),
                ("o5", 3, 0, 3),
                ("o6", 4, 0, 4),
                ("o6", 4, 1, None),
                ("o9", None, 5, None),
            ],
        )
        # 证据：主行 = [订单证据, 计入的发货证据...]，按序。
        self.assertEqual(
            records[0]["evidence"],
            [[str(self.orders), 2], [str(self.fulfillments), 2]],
        )
        self.assertEqual(
            records[6]["evidence"],
            [[str(self.orders), 7], [str(self.fulfillments), 6]],
        )
        self.assertEqual(
            records[7]["evidence"],
            [[str(self.fulfillments), 7]],
        )

    def test_same_key_shipments_sum_and_orphans_sorted(self) -> None:
        self.write_inputs(
            "o1,1,sku-a,10,open\n",
            "s2,o1,1,sku-a,3\n"
            "s1,o1,1,sku-a,4\n"
            "s4,zz,9,sku-a,1\n"
            "s3,zz,8,sku-a,1\n",
        )
        result = self.run_reconcile()
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.read_report()
        self.assertEqual(records[0]["kind"], "under_shipped")
        self.assertEqual(records[0]["shipped_qty"], 7)
        self.assertEqual(
            records[0]["evidence"],
            [
                [str(self.orders), 2],
                [str(self.fulfillments), 2],
                [str(self.fulfillments), 3],
            ],
        )
        # 孤立发货按 shipment_id 排序。
        self.assertEqual([r["kind"] for r in records[1:]],
                         ["missing_order", "missing_order"])
        self.assertEqual(
            [r["evidence"][0][1] for r in records[1:]],
            [5, 4],
        )

    def test_invalid_qty_fails_without_output(self) -> None:
        self.write_inputs("o1,1,sku-a,0,open\n", "s1,o1,1,sku-a,1\n")
        result = self.run_reconcile()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.orders), result.stderr)
        self.assertIn(":2:", result.stderr)
        self.assertFalse(self.output.exists())

    def test_duplicate_order_key_fails(self) -> None:
        self.write_inputs(
            "o1,1,sku-a,1,open\no1,1,sku-b,2,open\n",
            "s1,o1,1,sku-a,1\n",
        )
        result = self.run_reconcile()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.orders), result.stderr)
        self.assertIn(":3:", result.stderr)
        self.assertFalse(self.output.exists())

    def test_duplicate_shipment_id_fails(self) -> None:
        self.write_inputs(
            "o1,1,sku-a,2,open\n",
            "s1,o1,1,sku-a,1\ns1,o1,1,sku-a,1\n",
        )
        result = self.run_reconcile()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.fulfillments), result.stderr)
        self.assertIn(":3:", result.stderr)
        self.assertFalse(self.output.exists())

    def test_failure_leaves_existing_output_untouched(self) -> None:
        self.output.write_text("previous\n", encoding="utf-8")
        self.write_inputs("o1,1,sku-a,1,unknown\n", "s1,o1,1,sku-a,1\n")
        result = self.run_reconcile()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous\n")

    def test_bad_header_fails(self) -> None:
        self.orders.write_text("a,b,c\n", encoding="utf-8")
        self.fulfillments.write_text(FULFILLMENTS_HEADER, encoding="utf-8")
        result = self.run_reconcile()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.orders), result.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_input_file_fails(self) -> None:
        result = self.run_reconcile()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.orders), result.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_required_option_is_an_error(self) -> None:
        result = self.invoke("reconcile", "--orders", str(self.orders))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--fulfillments", result.stderr)


if __name__ == "__main__":
    unittest.main()
