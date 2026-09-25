"""Checks for the ``reconcile-fulfillments`` subcommand."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.orders_audit import AuditError
from ops_workbench.reconcile import OUTCOMES, reconcile, run_reconcile

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = json.dumps(
    {
        "order_id": ["order_id", "oid"],
        "sku": ["sku"],
        "qty": ["qty", "quantity"],
        "status": ["status"],
        "updated_at": ["updated_at", "ts"],
    }
)
HEADER = "oid,sku,qty,status,updated_at,note\n"
TS = "2024-01-02T03:04:05Z"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ops_workbench", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def parse_lines(payload: str) -> list[list]:
    return [json.loads(line) for line in payload.splitlines()]


def order_row(oid, sku, qty, status="open", note="x"):
    return f"{oid},{sku},{qty},{status},{TS},{note}\n"


def fulfillment_row(oid, sku, qty, status="shipped", note="y"):
    return f"{oid},{sku},{qty},{status},{TS},{note}\n"


class ReconcileCoreTests(unittest.TestCase):
    def reconcile(self, orders: str, fulfillments: str) -> list[list]:
        orders_data = orders.encode("utf-8")
        fulfillments_data = fulfillments.encode("utf-8")
        items = reconcile(
            orders_data, fulfillments_data, SCHEMA, "orders.csv", "fulfill.csv"
        )
        summary = items[-1]
        self.assertEqual(summary[0], "summary")
        self.assertEqual(summary[1], len(items) - 1)
        self.assertEqual(list(summary[2]), list(OUTCOMES))
        self.assertEqual(sum(summary[2].values()), summary[1])
        self.assertEqual(summary[3], hashlib.sha256(orders_data).hexdigest())
        self.assertEqual(summary[4], hashlib.sha256(fulfillments_data).hexdigest())
        return items

    def test_all_six_outcomes(self) -> None:
        orders = HEADER + "".join(
            [
                order_row("A1", "S1", 3),                      # balanced
                order_row("A2", "S2", 5),                      # under
                order_row("A3", "S3", 4),                      # over
                order_row("A4", "S4", 7),                      # no-fulfillment
                order_row("A5", "S5", 2, status="cancelled"),  # cancelled-only
            ]
        )
        fulfillments = HEADER + "".join(
            [
                fulfillment_row("A1", "S1", 3),
                fulfillment_row("A2", "S2", 2),
                fulfillment_row("A3", "S3", 6),
                fulfillment_row("A5", "S5", 9, status="cancelled"),
                fulfillment_row("A6", "S6", 1),  # orphan-fulfillment
            ]
        )
        items = self.reconcile(orders, fulfillments)
        body = items[:-1]
        self.assertEqual(
            body,
            [
                ["reconcile", "A1", "S1", 3, 3, "balanced"],
                ["reconcile", "A2", "S2", 5, 2, "under"],
                ["reconcile", "A3", "S3", 4, 6, "over"],
                ["reconcile", "A4", "S4", 7, 0, "no-fulfillment"],
                ["reconcile", "A5", "S5", 0, 0, "cancelled-only"],
                ["reconcile", "A6", "S6", 0, 1, "orphan-fulfillment"],
            ],
        )
        counts = items[-1][2]
        self.assertEqual(
            counts,
            {
                "cancelled-only": 1,
                "orphan-fulfillment": 1,
                "no-fulfillment": 1,
                "balanced": 1,
                "under": 1,
                "over": 1,
            },
        )

    def test_quantities_sum_across_rows(self) -> None:
        orders = HEADER + order_row("A1", "S1", 2) + order_row("A1", "S1", 3)
        fulfillments = (
            HEADER
            + fulfillment_row("A1", "S1", 4)
            + fulfillment_row("A1", "S1", 1, status="cancelled")
        )
        items = self.reconcile(orders, fulfillments)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 5, 4, "under"])

    def test_cancelled_rows_still_create_keys(self) -> None:
        orders = HEADER + order_row("A1", "S1", 5, status="cancelled")
        fulfillments = HEADER
        items = self.reconcile(orders, fulfillments)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 0, 0, "cancelled-only"])

    def test_identifiers_are_trimmed(self) -> None:
        orders = HEADER + order_row(" A1 ", " S1 ", 3)
        fulfillments = HEADER + fulfillment_row("A1", "S1", 3)
        items = self.reconcile(orders, fulfillments)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 3, 3, "balanced"])

    def test_keys_sort_ascending(self) -> None:
        orders = HEADER + order_row("B2", "S1", 1) + order_row("B1", "S9", 1)
        fulfillments = HEADER + fulfillment_row("B1", "S10", 1)
        items = self.reconcile(orders, fulfillments)
        self.assertEqual(
            [(item[1], item[2]) for item in items[:-1]],
            [("B1", "S10"), ("B1", "S9"), ("B2", "S1")],
        )

    def test_empty_inputs_emit_summary_only(self) -> None:
        items = self.reconcile(HEADER, HEADER)
        self.assertEqual(len(items), 1)
        summary = items[0]
        self.assertEqual(summary[0], "summary")
        self.assertEqual(summary[1], 0)
        self.assertEqual(summary[2], {outcome: 0 for outcome in OUTCOMES})

    def test_bom_is_accepted(self) -> None:
        orders = ("\ufeff" + HEADER + order_row("A1", "S1", 1)).encode("utf-8")
        fulfillments = (HEADER + fulfillment_row("A1", "S1", 1)).encode("utf-8")
        items = reconcile(orders, fulfillments, SCHEMA, "o.csv", "f.csv")
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 1, 1, "balanced"])

    def test_blank_records_are_skipped(self) -> None:
        orders = HEADER + "\n   \n" + order_row("A1", "S1", 1)
        fulfillments = HEADER + fulfillment_row("A1", "S1", 1)
        items = self.reconcile(orders, fulfillments)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 1, 1, "balanced"])

    def test_orders_reject_shipped_status(self) -> None:
        with self.assertRaises(AuditError):
            self.reconcile(HEADER + order_row("A1", "S1", 1, status="shipped"), HEADER)

    def test_fulfillments_reject_open_status(self) -> None:
        with self.assertRaises(AuditError):
            self.reconcile(HEADER, HEADER + fulfillment_row("A1", "S1", 1, status="open"))

    def test_field_value_errors_are_fatal(self) -> None:
        for row in [
            "A1,S1,0,open," + TS + ",x\n",          # bad qty
            "A1,S1,1,open,not-a-timestamp,x\n",      # bad updated_at
            ",S1,1,open," + TS + ",x\n",             # empty order_id
        ]:
            with self.subTest(row=row):
                with self.assertRaises(AuditError):
                    self.reconcile(HEADER + row, HEADER)

    def test_bad_schema(self) -> None:
        with self.assertRaises(AuditError):
            reconcile(b"a,b\n", b"a,b\n", "not json", "o.csv", "f.csv")

    def test_bad_encoding(self) -> None:
        with self.assertRaises(AuditError):
            reconcile(b"\xff\xfe", b"a,b\n", SCHEMA, "o.csv", "f.csv")

    def test_row_width_mismatch(self) -> None:
        with self.assertRaises(AuditError):
            self.reconcile(HEADER + "A1,S1,1\n", HEADER)

    def test_duplicate_header_names(self) -> None:
        header = "oid,sku,qty,status,updated_at,updated_at\n"
        with self.assertRaises(AuditError):
            self.reconcile(header, HEADER)


class ReconcileCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.orders = self.dir / "orders.csv"
        self.fulfillments = self.dir / "fulfill.csv"
        self.orders.write_text(HEADER + order_row("A1", "S1", 3), encoding="utf-8")
        self.fulfillments.write_text(
            HEADER + fulfillment_row("A1", "S1", 3), encoding="utf-8"
        )

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run_cli(
            "reconcile-fulfillments", "--schema", SCHEMA, *arguments
        )

    def test_stdout_and_exit_zero(self) -> None:
        result = self.invoke(str(self.orders), str(self.fulfillments))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        items = parse_lines(result.stdout)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 3, 3, "balanced"])
        self.assertEqual(items[-1][0], "summary")

    def test_output_file_is_written_atomically(self) -> None:
        report = self.dir / "report.jsonl"
        result = self.invoke(
            str(self.orders), str(self.fulfillments), "--output", str(report)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        items = parse_lines(report.read_text(encoding="utf-8"))
        self.assertEqual(items[0][0], "reconcile")
        # No temporary files are left behind.
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["fulfill.csv", "orders.csv", "report.jsonl"],
        )

    def test_failed_run_keeps_old_output(self) -> None:
        report = self.dir / "report.jsonl"
        report.write_text("previous\n", encoding="utf-8")
        self.fulfillments.write_text(
            HEADER + fulfillment_row("A1", "S1", 1, status="open"), encoding="utf-8"
        )
        result = self.invoke(
            str(self.orders), str(self.fulfillments), "--output", str(report)
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("status", result.stderr)
        self.assertEqual(report.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["fulfill.csv", "orders.csv", "report.jsonl"],
        )

    def test_field_value_error_exits_2_without_report(self) -> None:
        self.orders.write_text(
            HEADER + order_row("A1", "S1", 3, status="shipped"), encoding="utf-8"
        )
        result = self.invoke(str(self.orders), str(self.fulfillments))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("shipped", result.stderr)

    def test_missing_input_exits_2(self) -> None:
        result = self.invoke(str(self.dir / "missing.csv"), str(self.fulfillments))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotEqual(result.stderr, "")

    def test_schema_error_exits_2(self) -> None:
        result = run_cli(
            "reconcile-fulfillments",
            "--schema",
            "{}",
            str(self.orders),
            str(self.fulfillments),
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--schema", result.stderr)


if __name__ == "__main__":
    unittest.main()
