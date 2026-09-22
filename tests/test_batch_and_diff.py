"""Checks for batch tracing (``--db``/``--batch``) and ``diff-audits``."""

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.diff_audits import diff_findings, finding_identity, run_diff
from ops_workbench.orders_audit import (
    AuditError,
    BatchConflictError,
    fetch_batch,
    run_audit,
)

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
CLEAN_ROW = "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
BAD_ROW = "A1,S1,0,open,2024-01-02T03:04:05Z,x\n"


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


class BatchTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.input.write_bytes((HEADER + CLEAN_ROW).encode())
        self.db = str(base / "batches.db")

    def stored_batches(self) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT batch_id, input_sha256, schema_json, findings_json "
                "FROM batches ORDER BY batch_id"
            ).fetchall()
        finally:
            conn.close()

    def test_clean_run_is_stored_and_idempotent(self) -> None:
        out = io.BytesIO()
        code = run_audit(SCHEMA, str(self.input), stdout=out, db_path=self.db,
                         batch_id="b1")
        self.assertEqual(code, 0)
        rows = self.stored_batches()
        self.assertEqual(len(rows), 1)
        batch_id, digest, schema_json, findings_json = rows[0]
        self.assertEqual(batch_id, "b1")
        self.assertEqual(
            digest,
            hashlib.sha256((HEADER + CLEAN_ROW).encode()).hexdigest(),
        )
        self.assertEqual(json.loads(schema_json)["qty"], ["qty", "quantity"])
        self.assertEqual(json.loads(findings_json), [])

        # An identical rerun is idempotent: same report, database untouched.
        again = io.BytesIO()
        code = run_audit(SCHEMA, str(self.input), stdout=again, db_path=self.db,
                         batch_id="b1")
        self.assertEqual(code, 0)
        self.assertEqual(again.getvalue(), out.getvalue())
        self.assertEqual(len(self.stored_batches()), 1)

    def test_findings_run_still_saves_and_exits_one(self) -> None:
        self.input.write_bytes((HEADER + BAD_ROW).encode())
        out = io.BytesIO()
        code = run_audit(SCHEMA, str(self.input), stdout=out, db_path=self.db,
                         batch_id="b1")
        self.assertEqual(code, 1)
        findings = json.loads(self.stored_batches()[0][3])
        self.assertEqual(findings, [["invalid", 2, "qty", "0"]])

    def test_conflicting_batch_exits_three_and_changes_nothing(self) -> None:
        output = Path(self.dir.name) / "report.jsonl"
        code = run_audit(SCHEMA, str(self.input), str(output), db_path=self.db,
                         batch_id="b1")
        self.assertEqual(code, 0)
        before_db = self.stored_batches()
        before_report = output.read_bytes()

        self.input.write_bytes((HEADER + BAD_ROW).encode())
        with self.assertRaises(BatchConflictError) as ctx:
            run_audit(SCHEMA, str(self.input), str(output), db_path=self.db,
                      batch_id="b1")
        self.assertEqual(ctx.exception.batch_id, "b1")
        self.assertIn("finding set", ctx.exception.differing)
        self.assertIn("input hash", ctx.exception.differing)
        self.assertEqual(self.stored_batches(), before_db)
        self.assertEqual(output.read_bytes(), before_report)

    def test_schema_only_difference_is_a_conflict(self) -> None:
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id="b1")
        self.assertEqual(code, 0)
        other_schema = json.dumps({
            "order_id": ["oid"], "sku": ["sku"], "qty": ["qty"],
            "status": ["status"], "updated_at": ["updated_at"],
        })
        with self.assertRaises(BatchConflictError) as ctx:
            run_audit(other_schema, str(self.input), stdout=io.BytesIO(),
                      db_path=self.db, batch_id="b1")
        self.assertEqual(ctx.exception.differing, ["schema"])

    def test_input_error_does_not_create_or_rewrite_batch(self) -> None:
        with self.assertRaises(AuditError):
            run_audit(SCHEMA, str(Path(self.dir.name) / "missing.csv"),
                      db_path=self.db, batch_id="b1")
        self.assertFalse(Path(self.db).exists())

        # A fatal scan error after a successful batch leaves it untouched.
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id="b1")
        self.assertEqual(code, 0)
        before = self.stored_batches()
        self.input.write_bytes(b"\xff not utf-8")
        with self.assertRaises(AuditError):
            run_audit(SCHEMA, str(self.input), db_path=self.db, batch_id="b1")
        self.assertEqual(self.stored_batches(), before)

    def test_failed_report_write_rolls_back_the_batch(self) -> None:
        # --output naming a directory fails the atomic replace; the batch
        # must not be stored either.
        output = Path(self.dir.name) / "report.jsonl"
        output.mkdir()
        with self.assertRaises(AuditError):
            run_audit(SCHEMA, str(self.input), str(output), db_path=self.db,
                      batch_id="b1")
        conn = sqlite3.connect(self.db)
        try:
            try:
                rows = conn.execute("SELECT COUNT(*) FROM batches").fetchone()
            except sqlite3.OperationalError:
                rows = (0,)  # the CREATE TABLE was rolled back too
        finally:
            conn.close()
        self.assertEqual(rows[0], 0)

    def test_database_error_is_fatal(self) -> None:
        # A directory where the database file should be cannot be opened.
        with self.assertRaises(AuditError):
            run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                      db_path=self.dir.name, batch_id="b1")

    def test_cli_pairing_and_conflict_exit_codes(self) -> None:
        for extra in [("--db", self.db), ("--batch", "b1")]:
            with self.subTest(extra=extra):
                result = run_cli("audit-orders", "--schema", SCHEMA,
                                 str(self.input), *extra)
                self.assertEqual(result.returncode, 2)
                self.assertIn("together", result.stderr)
                self.assertEqual(result.stdout, "")

        ok = run_cli("audit-orders", "--schema", SCHEMA, str(self.input),
                     "--db", self.db, "--batch", "b1")
        self.assertEqual(ok.returncode, 0, ok.stderr)

        self.input.write_bytes((HEADER + BAD_ROW).encode())
        conflict = run_cli("audit-orders", "--schema", SCHEMA, str(self.input),
                           "--db", self.db, "--batch", "b1")
        self.assertEqual(conflict.returncode, 3)
        self.assertIn("b1", conflict.stderr)
        self.assertEqual(conflict.stdout, "")


class DiffFindingsTests(unittest.TestCase):
    def test_identity_shapes(self) -> None:
        self.assertEqual(
            finding_identity(["invalid", 7, "qty", "0"]),
            ["invalid", 7, "qty"],
        )
        self.assertEqual(
            finding_identity(["duplicate", ["A1", "S1"], [2, 4]]),
            ["duplicate", "A1", "S1"],
        )
        self.assertEqual(
            finding_identity(["conflict", ["A1", "S1"], [2, 4]]),
            ["conflict", "A1", "S1"],
        )

    def test_added_resolved_changed_and_identical(self) -> None:
        old = [
            ["invalid", 2, "qty", "0"],
            ["duplicate", ["A1", "S1"], [2, 4]],
            ["conflict", ["B2", "S2"], [3, 5]],
        ]
        new = [
            ["invalid", 2, "qty", "0"],          # identical: omitted
            ["duplicate", ["A1", "S1"], [2, 6]],  # same identity, changed
            ["invalid", 9, "sku", ""],            # added
            # conflict B2/S2 resolved
        ]
        items = diff_findings(old, new)
        self.assertEqual(
            items,
            [
                ["resolved", ["conflict", "B2", "S2"],
                 ["conflict", ["B2", "S2"], [3, 5]]],
                ["changed", ["duplicate", "A1", "S1"],
                 ["duplicate", ["A1", "S1"], [2, 4]],
                 ["duplicate", ["A1", "S1"], [2, 6]]],
                ["added", ["invalid", 9, "sku"],
                 ["invalid", 9, "sku", ""]],
            ],
        )

    def test_results_are_sorted_by_identity(self) -> None:
        old = [["invalid", 3, "qty", "0"]]
        new = [
            ["conflict", ["Z9", "S1"], [2, 4]],
            ["duplicate", ["A1", "S1"], [2, 4]],
            ["invalid", 2, "sku", ""],
        ]
        items = diff_findings(old, new)
        self.assertEqual(
            [item[1] for item in items],
            [
                ["conflict", "Z9", "S1"],
                ["duplicate", "A1", "S1"],
                ["invalid", 2, "sku"],
                ["invalid", 3, "qty"],
            ],
        )


class DiffAuditsRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.db = str(base / "batches.db")
        self.input = base / "orders.csv"

    def store(self, batch_id: str, csv_text: str) -> str:
        self.input.write_bytes(csv_text.encode())
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id=batch_id)
        self.assertIn(code, (0, 1))
        return hashlib.sha256(csv_text.encode()).hexdigest()

    def test_diff_between_two_batches(self) -> None:
        old_hash = self.store("old", HEADER + BAD_ROW)
        new_hash = self.store("new", HEADER + CLEAN_ROW)
        out = io.BytesIO()
        code = run_diff(self.db, "old", "new", stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue().decode())
        self.assertEqual(
            lines,
            [
                ["resolved", ["invalid", 2, "qty"], ["invalid", 2, "qty", "0"]],
                ["summary", 0, 1, 0, old_hash, new_hash],
            ],
        )

    def test_diff_of_a_batch_with_itself_has_no_items(self) -> None:
        digest = self.store("b1", HEADER + BAD_ROW)
        out = io.BytesIO()
        code = run_diff(self.db, "b1", "b1", stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(
            parse_lines(out.getvalue().decode()),
            [["summary", 0, 0, 0, digest, digest]],
        )

    def test_missing_batch_is_fatal_without_partial_results(self) -> None:
        self.store("b1", HEADER + CLEAN_ROW)
        out = io.BytesIO()
        with self.assertRaises(AuditError) as ctx:
            run_diff(self.db, "b1", "nope", stdout=out)
        self.assertIn("nope", ctx.exception.message)
        self.assertEqual(out.getvalue(), b"")

        output = Path(self.dir.name) / "diff.jsonl"
        output.write_text("OLD CONTENT")
        with self.assertRaises(AuditError):
            run_diff(self.db, "nope", "b1", str(output))
        self.assertEqual(output.read_text(), "OLD CONTENT")

    def test_missing_database_is_fatal(self) -> None:
        with self.assertRaises(AuditError):
            run_diff(str(Path(self.dir.name) / "absent.db"), "a", "b",
                     stdout=io.BytesIO())

    def test_output_is_written_atomically(self) -> None:
        self.store("old", HEADER + BAD_ROW)
        self.store("new", HEADER + CLEAN_ROW)
        output = Path(self.dir.name) / "diff.jsonl"
        output.write_text("OLD CONTENT")
        code = run_diff(self.db, "old", "new", str(output))
        self.assertEqual(code, 0)
        lines = parse_lines(output.read_text())
        self.assertEqual(lines[-1][0], "summary")
        self.assertEqual(lines[-1][1:4], [0, 1, 0])
        leftovers = [
            p.name for p in Path(self.dir.name).iterdir()
            if p.name.startswith(".diff")
        ]
        self.assertEqual(leftovers, [])

    def test_cli_end_to_end(self) -> None:
        self.store("old", HEADER + BAD_ROW)
        self.store("new", HEADER + CLEAN_ROW)

        result = run_cli("diff-audits", "--db", self.db, "old", "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[0][0], "resolved")
        self.assertEqual(lines[-1][0], "summary")

        missing = run_cli("diff-audits", "--db", self.db, "old", "ghost")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("ghost", missing.stderr)
        self.assertEqual(missing.stdout, "")

        output = Path(self.dir.name) / "out.jsonl"
        to_file = run_cli("diff-audits", "--db", self.db, "old", "new",
                          "--output", str(output))
        self.assertEqual(to_file.returncode, 0, to_file.stderr)
        self.assertEqual(to_file.stdout, "")
        self.assertEqual(parse_lines(output.read_text())[-1][0], "summary")


if __name__ == "__main__":
    unittest.main()
