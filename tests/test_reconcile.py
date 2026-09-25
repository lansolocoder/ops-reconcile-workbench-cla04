"""Checks for the ``reconcile-fulfillments`` subcommand."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.orders_audit import AuditError
from ops_workbench.reconcile_fulfillments import OUTCOMES, reconcile

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


def order_row(oid: str, sku: str, qty: str, status: str = "open") -> str:
    return f"{oid},{sku},{qty},{status},{TS},x\n"


def fulfillment_row(oid: str, sku: str, qty: str, status: str = "shipped") -> str:
    return f"{oid},{sku},{qty},{status},{TS},y\n"


class ReconcileCoreTests(unittest.TestCase):
    def reconcile(
        self, orders: str, fulfillments: str
    ) -> tuple[list[list], bytes, bytes]:
        orders_data = orders.encode("utf-8")
        fulfillments_data = fulfillments.encode("utf-8")
        items = reconcile(
            orders_data, fulfillments_data, SCHEMA, "orders.csv", "fulfillments.csv"
        )
        self.assertEqual(
            items[-1],
            [
                "summary",
                len(items) - 1,
                {outcome: 0 for outcome in OUTCOMES}
                | {
                    item[5]: sum(1 for other in items[:-1] if other[5] == item[5])
                    for item in items[:-1]
                },
                hashlib.sha256(orders_data).hexdigest(),
                hashlib.sha256(fulfillments_data).hexdigest(),
            ],
        )
        return items, orders_data, fulfillments_data

    def test_all_six_outcomes(self) -> None:
        orders = HEADER + "".join(
            [
                order_row("A1", "S1", "3"),  # balanced
                order_row("A2", "S2", "5"),  # under
                order_row("A3", "S3", "2"),  # over
                order_row("A4", "S4", "7"),  # no-fulfillment
                order_row("A5", "S5", "4", "cancelled"),  # cancelled-only
            ]
        )
        fulfillments = HEADER + "".join(
            [
                fulfillment_row("A1", "S1", "3"),
                fulfillment_row("A2", "S2", "2"),
                fulfillment_row("A3", "S3", "9"),
                fulfillment_row("A5", "S5", "1", "cancelled"),
                fulfillment_row("A6", "S6", "8"),  # orphan-fulfillment
            ]
        )
        items, _, _ = self.reconcile(orders, fulfillments)
        self.assertEqual(
            items[:-1],
            [
                ["reconcile", "A1", "S1", 3, 3, "balanced"],
                ["reconcile", "A2", "S2", 5, 2, "under"],
                ["reconcile", "A3", "S3", 2, 9, "over"],
                ["reconcile", "A4", "S4", 7, 0, "no-fulfillment"],
                ["reconcile", "A5", "S5", 0, 0, "cancelled-only"],
                ["reconcile", "A6", "S6", 0, 8, "orphan-fulfillment"],
            ],
        )
        summary = items[-1]
        self.assertEqual(summary[1], 6)
        self.assertEqual(
            summary[2],
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
        orders = HEADER + order_row("A1", "S1", "2") + order_row("A1", "S1", "3")
        fulfillments = (
            HEADER + fulfillment_row("A1", "S1", "4") + fulfillment_row("A1", "S1", "1")
        )
        items, _, _ = self.reconcile(orders, fulfillments)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 5, 5, "balanced"])

    def test_cancelled_rows_contribute_keys_but_no_quantity(self) -> None:
        orders = HEADER + order_row("A1", "S1", "9", "cancelled")
        fulfillments = HEADER + fulfillment_row("A2", "S2", "9", "cancelled")
        items, _, _ = self.reconcile(orders, fulfillments)
        self.assertEqual(
            items[:-1],
            [
                ["reconcile", "A1", "S1", 0, 0, "cancelled-only"],
                ["reconcile", "A2", "S2", 0, 0, "cancelled-only"],
            ],
        )

    def test_identifiers_are_trimmed(self) -> None:
        orders = HEADER + order_row(" A1 ", " S1 ", "3")
        fulfillments = HEADER + fulfillment_row("A1", "S1", "3")
        items, _, _ = self.reconcile(orders, fulfillments)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 3, 3, "balanced"])

    def test_keys_sort_element_wise(self) -> None:
        orders = (
            HEADER
            + order_row("B1", "S1", "1")
            + order_row("A1", "S2", "1")
            + order_row("A1", "S1", "1")
        )
        items, _, _ = self.reconcile(orders, HEADER)
        self.assertEqual(
            [[item[1], item[2]] for item in items[:-1]],
            [["A1", "S1"], ["A1", "S2"], ["B1", "S1"]],
        )

    def test_empty_inputs_emit_only_summary(self) -> None:
        items, orders_data, fulfillments_data = self.reconcile(HEADER, HEADER)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1], 0)
        self.assertEqual(
            items[0][2], {outcome: 0 for outcome in OUTCOMES}
        )

    def test_bom_is_accepted_and_hashed(self) -> None:
        orders_data = b"\xef\xbb\xbf" + (HEADER + order_row("A1", "S1", "1")).encode()
        fulfillments_data = HEADER.encode()
        items = reconcile(
            orders_data, fulfillments_data, SCHEMA, "orders.csv", "fulfillments.csv"
        )
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 1, 0, "no-fulfillment"])
        self.assertEqual(items[-1][3], hashlib.sha256(orders_data).hexdigest())

    def test_blank_records_are_skipped(self) -> None:
        orders = HEADER + "\n   \n" + order_row("A1", "S1", "2")
        items, _, _ = self.reconcile(orders, HEADER)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 2, 0, "no-fulfillment"])


class ReconcileErrorTests(unittest.TestCase):
    def expect_error(self, orders: bytes, fulfillments: bytes, *fragments: str) -> None:
        with self.assertRaises(AuditError) as ctx:
            reconcile(orders, fulfillments, SCHEMA, "orders.csv", "fulfillments.csv")
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))

    def test_invalid_order_field_value_is_fatal(self) -> None:
        orders = (HEADER + order_row("A1", "S1", "0")).encode()
        self.expect_error(orders, HEADER.encode(), "record 2", "qty")

    def test_order_status_vocabulary(self) -> None:
        orders = (HEADER + order_row("A1", "S1", "1", "shipped")).encode()
        self.expect_error(orders, HEADER.encode(), "status", "shipped")

    def test_fulfillment_status_vocabulary(self) -> None:
        fulfillments = (HEADER + fulfillment_row("A1", "S1", "1", "open")).encode()
        self.expect_error(HEADER.encode(), fulfillments, "status", "open")

    def test_row_width_mismatch_is_fatal(self) -> None:
        orders = (HEADER + "A1,S1,3,open\n").encode()
        self.expect_error(orders, HEADER.encode(), "record 2", "4 fields")

    def test_bad_encoding_is_fatal(self) -> None:
        self.expect_error(
            HEADER.encode() + b"A1,S1,1,open,2024-01-02T03:04:05Z,\xff\n",
            HEADER.encode(),
            "UTF-8",
        )

    def test_schema_errors_are_fatal(self) -> None:
        with self.assertRaises(AuditError):
            reconcile(
                HEADER.encode(), HEADER.encode(), "not json", "orders.csv", "f.csv"
            )
        with self.assertRaises(AuditError):
            reconcile(
                HEADER.encode(),
                HEADER.encode(),
                json.dumps({"order_id": ["oid"]}),
                "orders.csv",
                "f.csv",
            )

    def test_header_errors_are_fatal(self) -> None:
        self.expect_error(
            b"oid,oid,qty,status,updated_at,note\n", HEADER.encode(), "duplicate"
        )
        self.expect_error(b"", HEADER.encode(), "empty")


class ReconcileCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.orders = self.dir / "orders.csv"
        self.fulfillments = self.dir / "fulfillments.csv"
        self.orders.write_text(HEADER + order_row("A1", "S1", "3"), encoding="utf-8")
        self.fulfillments.write_text(
            HEADER + fulfillment_row("A1", "S1", "3"), encoding="utf-8"
        )

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run_cli("reconcile-fulfillments", "--schema", SCHEMA, *arguments)

    def test_stdout_report_and_exit_zero(self) -> None:
        result = self.invoke(str(self.orders), str(self.fulfillments))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        items = parse_lines(result.stdout)
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 3, 3, "balanced"])
        self.assertEqual(items[-1][0], "summary")

    def test_output_file_is_written_atomically(self) -> None:
        output = self.dir / "report.jsonl"
        result = self.invoke(
            str(self.orders), str(self.fulfillments), "--output", str(output)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        items = parse_lines(output.read_text(encoding="utf-8"))
        self.assertEqual(items[0], ["reconcile", "A1", "S1", 3, 3, "balanced"])
        # No temporary files are left behind.
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["fulfillments.csv", "orders.csv", "report.jsonl"],
        )

    def test_failed_run_keeps_old_output(self) -> None:
        output = self.dir / "report.jsonl"
        output.write_text("previous\n", encoding="utf-8")
        self.fulfillments.write_text(
            HEADER + fulfillment_row("A1", "S1", "1", "open"), encoding="utf-8"
        )
        result = self.invoke(
            str(self.orders), str(self.fulfillments), "--output", str(output)
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("status", result.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["fulfillments.csv", "orders.csv", "report.jsonl"],
        )

    def test_missing_input_exits_two(self) -> None:
        result = self.invoke(str(self.dir / "missing.csv"), str(self.fulfillments))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("missing.csv", result.stderr)

    def test_schema_error_exits_two_without_report(self) -> None:
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
