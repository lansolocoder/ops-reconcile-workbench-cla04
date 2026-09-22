"""Checks for batch traceability (``--db/--batch``) and ``diff-audits``."""

import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.batches import BatchConflict, BatchMissing, BatchStore
from ops_workbench.diff_audits import diff_findings, finding_identity

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = json.dumps(
    {
        "order_id": ["oid"],
        "sku": ["sku"],
        "qty": ["qty"],
        "status": ["status"],
        "updated_at": ["updated_at"],
    }
)
SCHEMA_ALT = json.dumps(
    {
        "order_id": ["oid"],
        "sku": ["sku"],
        "qty": ["quantity"],
        "status": ["status"],
        "updated_at": ["updated_at"],
    }
)
HEADER = "oid,sku,qty,status,updated_at\n"

CLEAN = HEADER + "A1,S1,3,open,2024-01-02T03:04:05Z\n"
DIRTY = HEADER + "A1,S1,0,open,2024-01-02T03:04:05Z\n"


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


class BatchAuditCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)
        self.db = self.base / "batches.db"
        self.input = self.base / "orders.csv"
        self.input.write_bytes(CLEAN.encode())

    def audit(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return run_cli(
            "audit-orders", "--schema", SCHEMA, str(self.input), *extra
        )

    def test_first_run_stores_batch(self) -> None:
        result = self.audit("--db", str(self.db), "--batch", "b1")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[-1][0], "summary")
        with BatchStore(str(self.db), readonly=True) as store:
            digest, schema, findings = store.load("b1")
        self.assertEqual(digest, lines[-1][3])
        self.assertEqual(schema["qty"], ["qty"])
        self.assertEqual(findings, [])

    def test_identical_rerun_is_idempotent(self) -> None:
        first = self.audit("--db", str(self.db), "--batch", "b1")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.audit("--db", str(self.db), "--batch", "b1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)

    def test_findings_exit_code_preserved_with_batch(self) -> None:
        self.input.write_bytes(DIRTY.encode())
        result = self.audit("--db", str(self.db), "--batch", "b1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('"invalid"', result.stdout)

    def test_conflicting_input_exits_3_and_changes_nothing(self) -> None:
        self.assertEqual(
            self.audit("--db", str(self.db), "--batch", "b1").returncode, 0
        )
        output = self.base / "report.jsonl"
        output.write_text("OLD CONTENT")
        self.input.write_bytes(DIRTY.encode())
        result = run_cli(
            "audit-orders", "--schema", SCHEMA, str(self.input),
            "--db", str(self.db), "--batch", "b1",
            "--output", str(output),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("b1", result.stderr)
        self.assertIn("SHA-256", result.stderr)
        self.assertEqual(result.stdout, "")
        # The previous output file and the stored batch are untouched.
        self.assertEqual(output.read_text(), "OLD CONTENT")
        with BatchStore(str(self.db), readonly=True) as store:
            _, _, findings = store.load("b1")
        self.assertEqual(findings, [])

    def test_conflicting_schema_exits_3(self) -> None:
        # Both schemas resolve against the same header, so the scan succeeds
        # and only the stored schema differs.
        self.input.write_bytes(
            (
                "oid,sku,qty,quantity,status,updated_at\n"
                "A1,S1,3,3,open,2024-01-02T03:04:05Z\n"
            ).encode()
        )
        self.assertEqual(
            self.audit("--db", str(self.db), "--batch", "b1").returncode, 0
        )
        result = run_cli(
            "audit-orders", "--schema", SCHEMA_ALT, str(self.input),
            "--db", str(self.db), "--batch", "b1",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("schema", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_conflicting_findings_exits_3(self) -> None:
        self.assertEqual(
            self.audit("--db", str(self.db), "--batch", "b1").returncode, 0
        )
        # Tamper with the stored finding set; hash and schema still match.
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE audit_batches SET findings = ? WHERE batch_id = ?",
            (json.dumps([["invalid", 9, "qty", "0"]]), "b1"),
        )
        conn.commit()
        conn.close()
        result = self.audit("--db", str(self.db), "--batch", "b1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("finding set", result.stderr)

    def test_db_and_batch_must_be_paired(self) -> None:
        for extra in (["--db", str(self.db)], ["--batch", "b1"]):
            with self.subTest(extra=extra):
                result = self.audit(*extra)
                self.assertEqual(result.returncode, 2)
                self.assertIn("--db and --batch", result.stderr)

    def test_input_error_does_not_create_batch_or_db(self) -> None:
        missing = self.base / "missing.csv"
        result = run_cli(
            "audit-orders", "--schema", SCHEMA, str(missing),
            "--db", str(self.db), "--batch", "b1",
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())

    def test_scan_error_leaves_existing_batches_untouched(self) -> None:
        self.assertEqual(
            self.audit("--db", str(self.db), "--batch", "b1").returncode, 0
        )
        self.input.write_bytes(b"oid,sku\nbroken\n")
        result = self.audit("--db", str(self.db), "--batch", "b2")
        self.assertEqual(result.returncode, 2)
        with BatchStore(str(self.db), readonly=True) as store:
            store.load("b1")  # still present
            with self.assertRaises(BatchMissing):
                store.load("b2")

    def test_unwritable_db_is_exit_2(self) -> None:
        result = run_cli(
            "audit-orders", "--schema", SCHEMA, str(self.input),
            "--db", str(self.base), "--batch", "b1",  # a directory
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_failed_report_write_does_not_store_batch(self) -> None:
        # --output names a directory: the write fails after the scan, and
        # the batch must not remain in the database (exit 2, no rewrite).
        output = self.base / "report.jsonl"
        output.mkdir()
        result = run_cli(
            "audit-orders", "--schema", SCHEMA, str(self.input),
            "--db", str(self.db), "--batch", "b1",
            "--output", str(output),
        )
        self.assertEqual(result.returncode, 2)
        with BatchStore(str(self.db), readonly=True) as store:
            with self.assertRaises(BatchMissing):
                store.load("b1")


class DiffFindingsUnitTests(unittest.TestCase):
    def test_identity_shapes(self) -> None:
        self.assertEqual(
            finding_identity(["invalid", 7, "qty", "0"]),
            ["invalid", 7, "qty"],
        )
        self.assertEqual(
            finding_identity(["duplicate", ["A1", "S1"], [2, 3]]),
            ["duplicate", "A1", "S1"],
        )
        self.assertEqual(
            finding_identity(["conflict", ["A1", "S1"], [2, 3]]),
            ["conflict", "A1", "S1"],
        )

    def test_added_resolved_changed_and_omitted(self) -> None:
        old = [
            ["conflict", ["A1", "S1"], [2, 4]],       # kept identical
            ["duplicate", ["A1", "S1"], [2, 4]],      # changed (records)
            ["invalid", 5, "qty", "0"],               # resolved
        ]
        new = [
            ["conflict", ["A1", "S1"], [2, 4]],
            ["duplicate", ["A1", "S1"], [2, 4, 6]],
            ["invalid", 3, "sku", ""],                # added
        ]
        lines = diff_findings(old, "OLDHASH", new, "NEWHASH")
        self.assertEqual(
            lines,
            [
                [
                    "changed",
                    ["duplicate", "A1", "S1"],
                    ["duplicate", ["A1", "S1"], [2, 4]],
                    ["duplicate", ["A1", "S1"], [2, 4, 6]],
                ],
                ["added", ["invalid", 3, "sku"], ["invalid", 3, "sku", ""]],
                ["resolved", ["invalid", 5, "qty"], ["invalid", 5, "qty", "0"]],
                ["summary", 1, 1, 1, "OLDHASH", "NEWHASH"],
            ],
        )

    def test_results_sorted_by_identity_itemwise(self) -> None:
        old = [["invalid", 10, "qty", "0"], ["invalid", 2, "sku", ""]]
        new = [["invalid", 2, "qty", "0"], ["conflict", ["B", "S"], [3, 4]]]
        lines = diff_findings(old, "o", new, "n")
        identities = [line[1] for line in lines[:-1]]
        self.assertEqual(
            identities,
            [
                ["conflict", "B", "S"],
                ["invalid", 2, "qty"],
                ["invalid", 2, "sku"],
                ["invalid", 10, "qty"],
            ],
        )

    def test_identical_sets_emit_only_summary(self) -> None:
        findings = [["invalid", 2, "qty", "0"]]
        lines = diff_findings(findings, "h", list(findings), "h")
        self.assertEqual(lines, [["summary", 0, 0, 0, "h", "h"]])


class DiffAuditsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)
        self.db = self.base / "batches.db"

    def store(self, batch_id: str, csv_text: str) -> list[list]:
        path = self.base / f"{batch_id}.csv"
        path.write_bytes(csv_text.encode())
        result = run_cli(
            "audit-orders", "--schema", SCHEMA, str(path),
            "--db", str(self.db), "--batch", batch_id,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return parse_lines(result.stdout)

    def test_diff_between_two_batches(self) -> None:
        old_lines = self.store(
            "old",
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05Z\n"
            + "A1,S1,3,open,2024-01-02T03:04:05Z\n"
            + "B1,S9,0,open,2024-01-02T03:04:05Z\n",
        )
        new_lines = self.store(
            "new",
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05Z\n"
            + "X1,X9,9,open,2024-01-02T03:04:05Z\n"
            + "\n"
            + "A1,S1,3,open,2024-01-02T03:04:05Z\n"
            + "C1,S2,5,cancelled,2024-01-02T03:04:05Z\n"
            + "C1,S2,5,cancelled,2024-01-02T03:04:05Z\n",
        )
        result = run_cli("diff-audits", "--db", str(self.db), "old", "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = parse_lines(result.stdout)
        self.assertEqual(
            lines,
            [
                [
                    "changed",
                    ["duplicate", "A1", "S1"],
                    ["duplicate", ["A1", "S1"], [2, 3]],
                    ["duplicate", ["A1", "S1"], [2, 5]],
                ],
                [
                    "added",
                    ["duplicate", "C1", "S2"],
                    ["duplicate", ["C1", "S2"], [6, 7]],
                ],
                ["resolved", ["invalid", 4, "qty"], ["invalid", 4, "qty", "0"]],
                ["summary", 1, 1, 1, old_lines[-1][3], new_lines[-1][3]],
            ],
        )

    def test_missing_batch_exits_2_without_partial_output(self) -> None:
        self.store("only", CLEAN)
        for args in [("only", "nope"), ("nope", "only"), ("nope", "nah")]:
            with self.subTest(args=args):
                result = run_cli("diff-audits", "--db", str(self.db), *args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("not found", result.stderr)

    def test_missing_db_file_exits_2_without_creating_it(self) -> None:
        db = self.base / "absent.db"
        result = run_cli("diff-audits", "--db", str(db), "a", "b")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(db.exists())

    def test_output_flag_writes_atomically(self) -> None:
        self.store("a", CLEAN)
        self.store("b", DIRTY)
        output = self.base / "diff.jsonl"
        output.write_text("OLD CONTENT")
        result = run_cli(
            "diff-audits", "--db", str(self.db), "a", "b",
            "--output", str(output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        lines = parse_lines(output.read_text())
        self.assertEqual(lines[0][0], "added")
        self.assertEqual(lines[-1][0], "summary")
        leftovers = [
            p.name for p in self.base.iterdir() if p.name.startswith(".diff")
        ]
        self.assertEqual(leftovers, [])

    def test_failed_output_write_preserves_existing_file(self) -> None:
        self.store("a", CLEAN)
        self.store("b", DIRTY)
        output = self.base / "diff.jsonl"
        output.mkdir()
        result = run_cli(
            "diff-audits", "--db", str(self.db), "a", "b",
            "--output", str(output),
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(output.is_dir())


if __name__ == "__main__":
    unittest.main()
