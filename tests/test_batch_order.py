"""Checks for the ``batch-order`` subcommand."""

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.batch_order import run_batch_order
from ops_workbench.orders_audit import AuditError, run_audit

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

SHA_A = hashlib.sha256(b"corrected-a").hexdigest()
SHA_B = hashlib.sha256(b"corrected-b").hexdigest()


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


class BatchOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.db = str(base / "batches.db")
        self.input = base / "orders.csv"
        self.input.write_bytes((HEADER + CLEAN_ROW).encode())

    def store_source(self, batch_id: str) -> str:
        code = run_audit(
            SCHEMA, str(self.input), stdout=io.BytesIO(),
            db_path=self.db, batch_id=batch_id,
        )
        self.assertEqual(code, 0)
        return hashlib.sha256((HEADER + CLEAN_ROW).encode()).hexdigest()

    def store_derived(
        self, derived_id: str, source_id: str, sha: str = SHA_A
    ) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS derived_batches ("
                "derived_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, "
                "schema_json TEXT NOT NULL, findings_json TEXT NOT NULL, "
                "source_batch_id TEXT NOT NULL, snapshot_json TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO derived_batches "
                "(derived_id, input_sha256, schema_json, findings_json, "
                "source_batch_id, snapshot_json) "
                "VALUES (?, ?, '[]', '[]', ?, '[]')",
                (derived_id, sha, source_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_source_batches_explain_themselves(self) -> None:
        digest = self.store_source("b1")
        self.store_source("b2")
        out = io.BytesIO()
        code = run_batch_order(self.db, ["b2", "b1"], stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(
            parse_lines(out.getvalue().decode()),
            [
                ["b2", "source", "b2", None, digest],
                ["b1", "source", "b1", None, digest],
                ["summary", 2, 2, 0, [digest]],
            ],
        )

    def test_derived_chain_walks_up_to_the_source(self) -> None:
        digest = self.store_source("base")
        self.store_derived("d1", "base", SHA_A)
        self.store_derived("d2", "d1", SHA_B)
        out = io.BytesIO()
        code = run_batch_order(self.db, ["d2", "d1", "base"], stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(
            parse_lines(out.getvalue().decode()),
            [
                ["d2", "derived", "base", "d1", SHA_B],
                ["d1", "derived", "base", "base", SHA_A],
                ["base", "source", "base", None, digest],
                ["summary", 3, 1, 2, sorted({digest, SHA_A, SHA_B})],
            ],
        )

    def test_broken_chain_yields_null_origin_but_verbatim_parent(self) -> None:
        self.store_source("base")
        # d1's parent row was lost; d2's parent never existed at all.
        self.store_derived("d1", "ghost", SHA_A)
        self.store_derived("d2", "d1", SHA_B)
        out = io.BytesIO()
        code = run_batch_order(self.db, ["d1", "d2"], stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(
            parse_lines(out.getvalue().decode()),
            [
                ["d1", "derived", None, "ghost", SHA_A],
                ["d2", "derived", None, "d1", SHA_B],
                ["summary", 2, 0, 2, sorted({SHA_A, SHA_B})],
            ],
        )

    def test_unknown_batch_is_fatal_without_partial_output(self) -> None:
        self.store_source("b1")
        out = io.BytesIO()
        with self.assertRaises(AuditError) as ctx:
            run_batch_order(self.db, ["b1", "nope"], stdout=out)
        self.assertIn("nope", ctx.exception.message)
        self.assertEqual(out.getvalue(), b"")

        output = Path(self.dir.name) / "order.jsonl"
        output.write_text("OLD CONTENT")
        with self.assertRaises(AuditError):
            run_batch_order(self.db, ["nope", "b1"], str(output))
        self.assertEqual(output.read_text(), "OLD CONTENT")

    def test_missing_database_is_fatal(self) -> None:
        with self.assertRaises(AuditError):
            run_batch_order(
                str(Path(self.dir.name) / "absent.db"), ["a", "b"],
                stdout=io.BytesIO(),
            )

    def test_fewer_than_two_ids_is_rejected(self) -> None:
        self.store_source("b1")
        with self.assertRaises(AuditError):
            run_batch_order(self.db, ["b1"], stdout=io.BytesIO())
        result = run_cli("batch-order", "--db", self.db, "b1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("at least two", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_output_is_written_atomically(self) -> None:
        digest = self.store_source("b1")
        self.store_derived("d1", "b1", SHA_A)
        output = Path(self.dir.name) / "order.jsonl"
        output.write_text("OLD CONTENT")
        code = run_batch_order(self.db, ["b1", "d1"], str(output))
        self.assertEqual(code, 0)
        lines = parse_lines(output.read_text())
        self.assertEqual(
            lines,
            [
                ["b1", "source", "b1", None, digest],
                ["d1", "derived", "b1", "b1", SHA_A],
                ["summary", 2, 1, 1, sorted({digest, SHA_A})],
            ],
        )
        leftovers = [
            p.name for p in Path(self.dir.name).iterdir()
            if p.name.startswith(".order")
        ]
        self.assertEqual(leftovers, [])

    def test_cli_end_to_end(self) -> None:
        self.store_source("b1")
        self.store_derived("d1", "b1", SHA_A)

        result = run_cli("batch-order", "--db", self.db, "b1", "d1")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[0][1], "source")
        self.assertEqual(lines[1][1], "derived")
        self.assertEqual(lines[-1][0], "summary")

        missing = run_cli("batch-order", "--db", self.db, "b1", "ghost")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("ghost", missing.stderr)
        self.assertEqual(missing.stdout, "")

        output = Path(self.dir.name) / "out.jsonl"
        to_file = run_cli(
            "batch-order", "--db", self.db, "b1", "d1",
            "--output", str(output),
        )
        self.assertEqual(to_file.returncode, 0, to_file.stderr)
        self.assertEqual(to_file.stdout, "")
        self.assertEqual(parse_lines(output.read_text())[-1][0], "summary")


if __name__ == "__main__":
    unittest.main()
