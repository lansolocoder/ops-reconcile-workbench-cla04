"""Checks for the reconcile subcommand."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

ORDERS_HEADER = "order_id,line_id,sku,ordered_qty,status\n"
FULFILLMENTS_HEADER = "shipment_id,order_id,line_id,sku,shipped_qty\n"


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.orders_path = self.tmp / "orders.csv"
        self.fulfillments_path = self.tmp / "fulfillments.csv"
        self.output_path = self.tmp / "report.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def invoke(
        self,
        orders: Path | None = None,
        fulfillments: Path | None = None,
        output: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ops_workbench",
                "reconcile",
                "--orders",
                str(self.orders_path if orders is None else orders),
                "--fulfillments",
                str(self.fulfillments_path if fulfillments is None else fulfillments),
                "--output",
                str(self.output_path if output is None else output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def write(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def run_ok(self, orders: str, fulfillments: str) -> list[dict]:
        self.write(self.orders_path, orders)
        self.write(self.fulfillments_path, fulfillments)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = self.output_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def test_all_record_kinds_and_evidence(self) -> None:
        orders = (
            ORDERS_HEADER
            + "O1,L1,SKU-A,5,open\n"       # matched
            + "O1,L2,SKU-B,5,open\n"       # under_shipped
            + "O2,L1,SKU-C,2,open\n"       # over_shipped
            + "O3,L1,SKU-D,3,open\n"       # sku_conflict, other F covers 3
            + "O4,L1,SKU-E,4,cancelled\n"  # cancelled matched
            + "O5,L1,SKU-F,4,cancelled\n"  # cancelled_but_shipped
        )
        fulfillments = (
            FULFILLMENTS_HEADER
            + "S1,O1,L1,SKU-A,2\n"
            + "S2,O1,L1,SKU-A,3\n"
            + "S3,O1,L2,SKU-B,1\n"
            + "S4,O2,L1,SKU-C,9\n"
            + "S5,O3,L1,SKU-X,1\n"
            + "S6,O3,L1,SKU-D,3\n"
            + "S7,O5,L1,SKU-F,4\n"
            + "S8,O9,L9,SKU-Z,7\n"
        )
        records = self.run_ok(orders, fulfillments)

        by_kind = {}
        for record in records:
            by_kind.setdefault(record["kind"], []).append(record)
            self.assertEqual(
                list(record),
                [
                    "kind",
                    "order_id",
                    "line_id",
                    "ordered_qty",
                    "shipped_qty",
                    "difference",
                    "evidence",
                ],
            )

        matched = by_kind["matched"][0]
        self.assertEqual(
            matched,
            {
                "kind": "matched",
                "order_id": "O1",
                "line_id": "L1",
                "ordered_qty": 5,
                "shipped_qty": 5,
                "difference": 0,
                "evidence": [
                    ["orders.csv", 2],
                    ["fulfillments.csv", 2],
                    ["fulfillments.csv", 3],
                ],
            },
        )

        under = by_kind["under_shipped"][0]
        self.assertEqual(under["ordered_qty"], 5)
        self.assertEqual(under["shipped_qty"], 1)
        self.assertEqual(under["difference"], 4)
        self.assertEqual(
            under["evidence"], [["orders.csv", 3], ["fulfillments.csv", 4]]
        )

        over = by_kind["over_shipped"][0]
        self.assertEqual(over["ordered_qty"], 2)
        self.assertEqual(over["shipped_qty"], 9)
        self.assertEqual(over["difference"], -7)

        conflict = by_kind["sku_conflict"][0]
        self.assertEqual(
            conflict,
            {
                "kind": "sku_conflict",
                "order_id": "O3",
                "line_id": "L1",
                "ordered_qty": 3,
                "shipped_qty": 1,
                "difference": None,
                "evidence": [
                    ["orders.csv", 5],
                    ["fulfillments.csv", 6],
                ],
            },
        )
        # The conflicting shipment must not count toward the main row.
        o3_main = next(
            r for r in records if r["order_id"] == "O3" and r["kind"] == "matched"
        )
        self.assertEqual(o3_main["shipped_qty"], 3)
        self.assertEqual(
            o3_main["evidence"],
            [["orders.csv", 5], ["fulfillments.csv", 7]],
        )

        cancelled_matched = next(
            r for r in by_kind["matched"] if r["order_id"] == "O4"
        )
        self.assertEqual(cancelled_matched["shipped_qty"], 0)
        self.assertEqual(cancelled_matched["difference"], 4)
        self.assertEqual(
            cancelled_matched["evidence"], [["orders.csv", 6]]
        )

        cancelled_shipped = by_kind["cancelled_but_shipped"][0]
        self.assertEqual(cancelled_shipped["order_id"], "O5")
        self.assertEqual(cancelled_shipped["shipped_qty"], 4)
        self.assertEqual(cancelled_shipped["difference"], 0)

        missing = by_kind["missing_order"][0]
        self.assertEqual(
            missing,
            {
                "kind": "missing_order",
                "order_id": "O9",
                "line_id": "L9",
                "ordered_qty": None,
                "shipped_qty": 7,
                "difference": None,
                "evidence": [["fulfillments.csv", 9]],
            },
        )

    def test_records_sorted_by_order_key_then_orphan_shipment_id(self) -> None:
        orders = (
            ORDERS_HEADER
            + "B,2,S,1,open\n"
            + "A,2,S,1,open\n"
            + "A,1,S,1,open\n"
        )
        fulfillments = (
            FULFILLMENTS_HEADER
            + "Z9,B,2,S,1\n"
            + "Z1,A,9,S,1\n"
            + "Z0,A,8,S,1\n"
        )
        # Linked rows sorted by order key; trailing orphans sorted by
        # shipment_id (Z0 on line 4 before Z1 on line 3).
        records = self.run_ok(orders, fulfillments)
        self.assertEqual(
            [(r["kind"], r["order_id"], r["line_id"]) for r in records],
            [
                ("under_shipped", "A", "1"),
                ("under_shipped", "A", "2"),
                ("matched", "B", "2"),
                ("missing_order", "A", "8"),
                ("missing_order", "A", "9"),
            ],
        )
        self.assertEqual(
            [r["evidence"][0][1] for r in records[-2:]], [4, 3]
        )

        # Orphans alone, ordered by shipment_id: shipA (line 3),
        # shipM (line 4), shipZ (line 2).
        fulfillments_orphans = (
            FULFILLMENTS_HEADER
            + "shipZ,O2,L1,S,1\n"
            + "shipA,O1,L1,S,1\n"
            + "shipM,O1,L2,S,1\n"
        )
        records = self.run_ok(ORDERS_HEADER, fulfillments_orphans)
        self.assertEqual(
            [(r["order_id"], r["evidence"][0][1]) for r in records],
            [("O1", 3), ("O1", 4), ("O2", 2)],
        )

    def test_empty_inputs_produce_empty_report(self) -> None:
        records = self.run_ok(ORDERS_HEADER, FULFILLMENTS_HEADER)
        self.assertEqual(records, [])
        self.assertEqual(self.output_path.read_text(encoding="utf-8"), "")

    def test_utf8_content_is_supported(self) -> None:
        orders = ORDERS_HEADER + "订单一,行1,商品α,2,open\n"
        fulfillments = FULFILLMENTS_HEADER + "发货1,订单一,行1,商品α,2\n"
        records = self.run_ok(orders, fulfillments)
        self.assertEqual(records[0]["kind"], "matched")
        self.assertEqual(records[0]["order_id"], "订单一")
        raw = self.output_path.read_text(encoding="utf-8")
        self.assertIn("订单一", raw)

    def assert_noncompliant(
        self, orders: str | None, fulfillments: str | None, marker: str
    ) -> None:
        if orders is not None:
            self.write(self.orders_path, orders)
        if fulfillments is not None:
            self.write(self.fulfillments_path, fulfillments)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(marker, result.stderr)
        self.assertFalse(self.output_path.exists())

    def test_missing_orders_file(self) -> None:
        self.write(self.fulfillments_path, FULFILLMENTS_HEADER)
        self.assert_noncompliant(None, None, "orders.csv")

    def test_missing_fulfillments_file(self) -> None:
        self.assert_noncompliant(ORDERS_HEADER, None, "fulfillments.csv")

    def test_bad_header_reports_line_1(self) -> None:
        self.assert_noncompliant(
            "order_id,line_id,sku,ordered_qty\n",
            FULFILLMENTS_HEADER,
            "orders.csv:1",
        )

    def test_wrong_column_count_reports_line(self) -> None:
        self.assert_noncompliant(
            ORDERS_HEADER + "O1,L1,S,3\n",
            FULFILLMENTS_HEADER,
            "orders.csv:2",
        )

    def test_non_positive_quantity_reports_line(self) -> None:
        for bad in ("0", "-3", "1.5", "abc", ""):
            with self.subTest(bad=bad):
                self.output_path.unlink(missing_ok=True)
                self.assert_noncompliant(
                    ORDERS_HEADER + f"O1,L1,S,{bad},open\n",
                    FULFILLMENTS_HEADER,
                    "orders.csv:2",
                )

    def test_bad_status_reports_line(self) -> None:
        self.assert_noncompliant(
            ORDERS_HEADER + "O1,L1,S,3,closed\n",
            FULFILLMENTS_HEADER,
            "orders.csv:2",
        )

    def test_empty_field_reports_line(self) -> None:
        self.assert_noncompliant(
            ORDERS_HEADER + "O1,,S,3,open\n",
            FULFILLMENTS_HEADER,
            "orders.csv:2",
        )

    def test_duplicate_order_key_reports_line(self) -> None:
        self.assert_noncompliant(
            ORDERS_HEADER
            + "O1,L1,S,3,open\n"
            + "O1,L1,S,4,open\n",
            FULFILLMENTS_HEADER,
            "orders.csv:3",
        )

    def test_duplicate_shipment_id_reports_line(self) -> None:
        self.assert_noncompliant(
            ORDERS_HEADER,
            FULFILLMENTS_HEADER
            + "S1,O1,L1,S,1\n"
            + "S1,O1,L2,S,1\n",
            "fulfillments.csv:3",
        )

    def test_failure_leaves_existing_report_unchanged(self) -> None:
        self.write(self.orders_path, ORDERS_HEADER + "O1,L1,S,1,open\n")
        self.write(
            self.fulfillments_path, FULFILLMENTS_HEADER + "S1,O1,L1,S,1\n"
        )
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.output_path.read_text(encoding="utf-8")

        # Now make the orders file invalid; the old report must survive.
        self.write(self.orders_path, ORDERS_HEADER + "O1,L1,S,zero,open\n")
        second = self.invoke()
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(self.output_path.read_text(encoding="utf-8"), before)

    def test_reconcile_requires_its_arguments(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "ops_workbench", "reconcile"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--orders", result.stderr)


if __name__ == "__main__":
    unittest.main()
