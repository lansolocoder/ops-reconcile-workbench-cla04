"""Checks for the ``batch-order`` batch provenance subcommand."""

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.batch_order import (
    build_batch_order,
    build_order_with_fixes,
    run_batch_order,
)
from ops_workbench.decisions import run_decide
from ops_workbench.fixes import run_apply_fixes, run_propose_fix
from ops_workbench.orders_audit import AuditError, run_audit

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
HEADER = "oid,sku,qty,status,updated_at\n"
TS = "2024-01-02T03:04:05Z"
CLEAN_ROW = "A1,S1,3,open,2024-01-02T03:04:05Z\n"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ops_workbench", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def parse_lines(payload: str | bytes) -> list[list]:
    if isinstance(payload, bytes):
        payload = payload.decode()
    return [json.loads(line) for line in payload.splitlines()]


class BatchOrderDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.db_path = base / "batches.db"
        self.input = base / "orders.csv"

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def store_source(self, batch_id: str, csv_text: str = HEADER + CLEAN_ROW) -> str:
        """Create a real source batch via audit-orders and return its hash."""
        self.input.write_bytes(csv_text.encode())
        code = run_audit(
            SCHEMA,
            str(self.input),
            stdout=io.BytesIO(),
            db_path=str(self.db_path),
            batch_id=batch_id,
        )
        self.assertIn(code, (0, 1))
        return hashlib.sha256(csv_text.encode()).hexdigest()

    def store_derived(
        self,
        derived_id: str,
        source_id: str,
        digest: str,
        snapshot: str = "[]",
    ) -> None:
        """Insert a derived_batches row with the minimum trace columns."""
        conn = self.connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS derived_batches ("
                "derived_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, "
                "schema_json TEXT NOT NULL, findings_json TEXT NOT NULL, "
                "source_batch_id TEXT NOT NULL, snapshot_json TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO derived_batches (derived_id, input_sha256, "
                "schema_json, findings_json, source_batch_id, snapshot_json) "
                "VALUES (?, ?, '{}', '[]', ?, ?)",
                (derived_id, digest, source_id, snapshot),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_derived(self, derived_id: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "DELETE FROM derived_batches WHERE derived_id = ?", (derived_id,)
            )
            conn.commit()
        finally:
            conn.close()

    def store_applied_derived(self, derived_id: str = "der") -> str:
        """Run a full decide/propose/apply-fixes flow; return the new hash.

        The source batch has one invalid-qty finding and one duplicate
        group, each with a fix decision and a proposal, so the derived
        snapshot carries two fix entries.
        """
        csv_text = (
            HEADER
            + f"A0,S0,0,open,{TS}\n"
            + f"A1,S1,2,open,{TS}\n"
            + f"A1,S1,2,open,{TS}\n"
        )
        self.input.write_bytes(csv_text.encode())
        code = run_audit(
            SCHEMA,
            str(self.input),
            stdout=io.BytesIO(),
            db_path=str(self.db_path),
            batch_id="src",
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            run_decide(
                str(self.db_path), "src", '["invalid",2,"qty"]', "fix", "  r1  "
            ),
            0,
        )
        self.assertEqual(
            run_decide(
                str(self.db_path), "src", '["duplicate","A1","S1"]',
                "fix", "r2",
            ),
            0,
        )
        self.assertEqual(
            run_propose_fix(
                str(self.db_path), "src", '["invalid",2,"qty"]', '[[2,"qty","5"]]'
            ),
            0,
        )
        self.assertEqual(
            run_propose_fix(
                str(self.db_path), "src", '["duplicate","A1","S1"]',
                '[[3,"sku","S7"]]',
            ),
            0,
        )
        corrected = self.db_path.parent / "corrected.csv"
        code = run_apply_fixes(
            str(self.db_path), "src", derived_id, str(self.input),
            str(corrected), stdout=io.BytesIO(),
        )
        self.assertEqual(code, 0)
        return hashlib.sha256(corrected.read_bytes()).hexdigest()

    def overwrite_snapshot(self, derived_id: str, snapshot: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE derived_batches SET snapshot_json = ? WHERE derived_id = ?",
                (snapshot, derived_id),
            )
            conn.commit()
        finally:
            conn.close()


class BuildBatchOrderTests(unittest.TestCase):
    def test_source_and_derived_row_shapes(self) -> None:
        batches = {"s": "h0"}
        derived = {"d": ("h1", "s")}
        rows = build_batch_order(batches, derived, ["s", "d"])
        self.assertEqual(
            rows,
            [
                ["s", "source", "s", None, "h0"],
                ["d", "derived", "s", "s", "h1"],
                ["summary", 2, 1, 1, ["h0", "h1"]],
            ],
        )

    def test_source_id_walks_a_derived_chain(self) -> None:
        batches = {"root": "h0"}
        derived = {
            "d1": ("h1", "root"),
            "d2": ("h2", "d1"),
            "d3": ("h3", "d2"),
        }
        rows = build_batch_order(batches, derived, ["d3", "d2"])
        # The derived id is always the direct SOURCE; the source id walks to
        # the first non-derived batch in the chain.
        self.assertEqual(rows[0], ["d3", "derived", "root", "d2", "h3"])
        self.assertEqual(rows[1], ["d2", "derived", "root", "d1", "h2"])
        self.assertEqual(rows[2], ["summary", 2, 0, 2, ["h2", "h3"]])

    def test_chain_through_a_deleted_derived_row_has_null_source(self) -> None:
        batches = {"root": "h0"}
        derived = {"d2": ("h2", "d1")}  # direct source d1 is a missing row
        rows = build_batch_order(batches, derived, ["d2"])
        self.assertEqual(rows[0], ["d2", "derived", None, "d1", "h2"])
        # Kind counts follow the row label, not the resolved source.
        self.assertEqual(rows[1], ["summary", 1, 0, 1, ["h2"]])

    def test_direct_source_completely_absent_has_null_source(self) -> None:
        rows = build_batch_order({}, {"d": ("h", "ghost")}, ["d"])
        self.assertEqual(rows[0], ["d", "derived", None, "ghost", "h"])

    def test_rows_follow_input_order_and_summary_hashes_dedup_sorted(self) -> None:
        batches = {"b": "h2", "a": "h1", "c": "h1"}
        rows = build_batch_order(batches, {}, ["c", "a", "b", "a"])
        self.assertEqual([r[0] for r in rows[:-1]], ["c", "a", "b", "a"])
        self.assertEqual(rows[-1], ["summary", 4, 4, 0, ["h1", "h2"]])


def _entry(identity: list, action: str, reason: str, patch: list) -> dict:
    """A decision/proposal snapshot entry shaped like apply-fixes stores it."""
    return {
        "identity": identity,
        "action": action,
        "reason": reason,
        "finding": list(identity),
        "proposal": {"finding": list(identity), "patch": patch},
    }


class BuildFixImpactTests(unittest.TestCase):
    def _derived(self, snapshot_text: str) -> dict:
        return {"d": ("hd", "s", snapshot_text)}

    def test_source_batch_emits_no_fix_rows(self) -> None:
        rows = build_order_with_fixes({"s": "h0"}, {}, ["s"])
        self.assertEqual(
            rows,
            [
                ["s", "source", "s", None, "h0"],
                ["summary", 1, 1, 0, ["h0"]],
            ],
        )

    def test_empty_snapshot_emits_only_the_provenance_row(self) -> None:
        rows = build_order_with_fixes({"s": "h0"}, self._derived("[]"), ["d"])
        self.assertEqual(
            rows,
            [
                ["d", "derived", "s", "s", "hd"],
                ["summary", 1, 0, 1, ["hd"]],
            ],
        )

    def test_fix_rows_follow_the_provenance_row_sorted_by_identity(self) -> None:
        snapshot = [
            _entry(["invalid", 5, "sku"], "fix", "  s  ", [[5, "sku", "S9"]]),
            _entry(["duplicate", "A1", "S1"], "fix", "d",
                   [[4, "sku", "S7"], [3, "qty", "8"]]),
        ]
        rows = build_order_with_fixes(
            {"s": "h0"}, self._derived(json.dumps(snapshot)), ["d"]
        )
        self.assertEqual(
            rows,
            [
                ["d", "derived", "s", "s", "hd"],
                ["fix", "d", ["duplicate", "A1", "S1"], ["fix", "d"],
                 [3, "qty", "8", 4, "sku", "S7"]],
                ["fix", "d", ["invalid", 5, "sku"], ["fix", "s"],
                 [5, "sku", "S9"]],
                ["summary", 1, 0, 1, ["hd"]],
            ],
        )

    def test_snapshot_need_not_be_presorted(self) -> None:
        snapshot = [
            _entry(["invalid", 9, "qty"], "fix", "b", [[9, "qty", "1"]]),
            _entry(["invalid", 2, "qty"], "fix", "a", [[2, "qty", "5"]]),
            _entry(["conflict", "A1", "S1"], "fix", "c", [[3, "sku", "Z"]]),
        ]
        rows = build_order_with_fixes(
            {"s": "h0"}, self._derived(json.dumps(snapshot)), ["d"]
        )
        fix_identities = [row[2] for row in rows if row[0] == "fix"]
        self.assertEqual(
            fix_identities,
            [["conflict", "A1", "S1"], ["invalid", 2, "qty"],
             ["invalid", 9, "qty"]],
        )

    def test_patch_triples_merge_sorted_by_record_then_field(self) -> None:
        snapshot = [
            _entry(["duplicate", "A1", "S1"], "fix", "d",
                   [[4, "sku", "S7"], [3, "status", "open"], [4, "qty", "8"]]),
        ]
        rows = build_order_with_fixes(
            {"s": "h0"}, self._derived(json.dumps(snapshot)), ["d"]
        )
        self.assertEqual(
            rows[1][4],
            [3, "status", "open", 4, "qty", "8", 4, "sku", "S7"],
        )

    def test_multiple_derived_batches_keep_input_order(self) -> None:
        snapshot = [_entry(["invalid", 2, "qty"], "fix", "r", [[2, "qty", "5"]])]
        derived = {
            "d1": ("h1", "s", json.dumps(snapshot)),
            "d2": ("h2", "d1", "[]"),
        }
        rows = build_order_with_fixes({"s": "h0"}, derived, ["d2", "d1", "s"])
        self.assertEqual(rows[0][0], "d2")
        self.assertEqual(rows[1][0], "d1")
        self.assertEqual(rows[2], ["fix", "d1", ["invalid", 2, "qty"],
                                   ["fix", "r"], [2, "qty", "5"]])
        self.assertEqual(rows[3][:2], ["s", "source"])
        self.assertEqual(rows[-1], ["summary", 3, 1, 2,
                                    sorted({"h0", "h1", "h2"})])

    def test_corrupt_snapshot_is_fatal(self) -> None:
        with self.assertRaises(AuditError):
            build_order_with_fixes({"s": "h0"}, self._derived("{nope"), ["d"])

    def test_incomplete_entries_are_fatal(self) -> None:
        good = _entry(["invalid", 2, "qty"], "fix", "r", [[2, "qty", "5"]])
        cases = []
        broken = dict(good)
        del broken["identity"]
        cases.append(broken)
        broken = dict(good)
        del broken["action"]
        cases.append(broken)
        broken = dict(good)
        del broken["proposal"]
        cases.append(broken)
        broken = dict(good)
        broken["proposal"] = {"finding": []}  # missing patch
        cases.append(broken)
        for bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(AuditError):
                    build_order_with_fixes(
                        {"s": "h0"}, self._derived(json.dumps([bad])), ["d"]
                    )


class RunBatchOrderTests(BatchOrderDatabase):
    def test_two_source_batches_end_to_end(self) -> None:
        h1 = self.store_source("b1")
        h2 = self.store_source("b2")
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["b2", "b1"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(
            lines,
            [
                ["b2", "source", "b2", None, h2],
                ["b1", "source", "b1", None, h1],
                ["summary", 2, 2, 0, sorted({h1, h2})],
            ],
        )

    def test_derived_chain_resolves_to_originating_source(self) -> None:
        h0 = self.store_source("root")
        self.store_derived("d1", "root", "h1")
        self.store_derived("d2", "d1", "h2")
        out = io.BytesIO()
        code = run_batch_order(
            str(self.db_path), ["d2", "d1", "root"], stdout=out
        )
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(
            lines,
            [
                ["d2", "derived", "root", "d1", "h2"],
                ["d1", "derived", "root", "root", "h1"],
                ["root", "source", "root", None, h0],
                ["summary", 3, 1, 2, sorted({h0, "h1", "h2"})],
            ],
        )

    def test_missing_chain_link_leaves_null_source_but_keeps_direct_id(self) -> None:
        self.store_source("root")
        self.store_derived("d1", "root", "h1")
        self.store_derived("d2", "d1", "h2")
        self.delete_derived("d1")
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["d2", "root"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(lines[0], ["d2", "derived", None, "d1", "h2"])
        self.assertEqual(lines[1][1], "source")
        self.assertEqual(lines[2], ["summary", 2, 1, 1, sorted({"h2", lines[1][4]})])

    def test_direct_source_that_never_existed_has_null_source(self) -> None:
        self.store_derived("d", "ghost", "h1")
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["d"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(lines[0], ["d", "derived", None, "ghost", "h1"])

    def test_applied_derived_emits_its_fix_impact(self) -> None:
        digest = self.store_applied_derived("der")
        source_hash = hashlib.sha256(
            (
                HEADER
                + f"A0,S0,0,open,{TS}\n"
                + f"A1,S1,2,open,{TS}\n"
                + f"A1,S1,2,open,{TS}\n"
            ).encode()
        ).hexdigest()
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["der", "src"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(
            lines,
            [
                ["der", "derived", "src", "src", digest],
                ["fix", "der", ["duplicate", "A1", "S1"], ["fix", "r2"],
                 [3, "sku", "S7"]],
                ["fix", "der", ["invalid", 2, "qty"], ["fix", "r1"],
                 [2, "qty", "5"]],
                ["src", "source", "src", None, source_hash],
                ["summary", 2, 1, 1, sorted({source_hash, digest})],
            ],
        )

    def test_corrupt_snapshot_is_fatal_without_partial_output(self) -> None:
        self.store_applied_derived("der")
        self.overwrite_snapshot("der", "{not json")
        out = io.BytesIO()
        with self.assertRaises(AuditError) as ctx:
            run_batch_order(str(self.db_path), ["der", "src"], stdout=out)
        self.assertIn("not valid JSON", ctx.exception.message)
        self.assertEqual(out.getvalue(), b"")

    def test_incomplete_snapshot_entry_is_fatal_and_keeps_old_output(self) -> None:
        self.store_applied_derived("der")
        conn = self.connect()
        try:
            snapshot = json.loads(
                conn.execute(
                    "SELECT snapshot_json FROM derived_batches "
                    "WHERE derived_id = 'der'"
                ).fetchone()[0]
            )
        finally:
            conn.close()
        del snapshot[0]["proposal"]
        self.overwrite_snapshot("der", json.dumps(snapshot))

        output = self.db_path.parent / "order.jsonl"
        output.write_text("OLD CONTENT")
        with self.assertRaises(AuditError) as ctx:
            run_batch_order(str(self.db_path), ["der", "src"], str(output))
        self.assertIn("no proposal", ctx.exception.message)
        self.assertEqual(output.read_text(), "OLD CONTENT")
        leftovers = [
            p.name
            for p in self.db_path.parent.iterdir()
            if p.name.startswith(".order")
        ]
        self.assertEqual(leftovers, [])

    def test_unknown_batch_is_fatal_without_partial_output(self) -> None:
        self.store_source("b1")
        self.store_derived("d", "b1", "h1")
        out = io.BytesIO()
        with self.assertRaises(AuditError) as ctx:
            run_batch_order(
                str(self.db_path), ["b1", "ghost", "d"], stdout=out
            )
        self.assertIn("ghost", ctx.exception.message)
        self.assertEqual(out.getvalue(), b"")

        output = self.db_path.parent / "order.jsonl"
        output.write_text("OLD CONTENT")
        with self.assertRaises(AuditError):
            run_batch_order(str(self.db_path), ["ghost", "b1"], str(output))
        self.assertEqual(output.read_text(), "OLD CONTENT")

    def test_database_without_derived_table_still_resolves_sources(self) -> None:
        digest = self.store_source("b1")
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["b1", "b1"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual([line[0] for line in lines[:-1]], ["b1", "b1"])
        self.assertEqual(lines[-1], ["summary", 2, 2, 0, [digest]])

    def test_missing_database_is_fatal(self) -> None:
        with self.assertRaises(AuditError):
            run_batch_order(
                str(self.db_path.parent / "absent.db"), ["a", "b"],
                stdout=io.BytesIO(),
            )

    def test_output_is_written_atomically_without_temp_leftovers(self) -> None:
        self.store_source("b1")
        self.store_source("b2")
        output = self.db_path.parent / "order.jsonl"
        output.write_text("OLD CONTENT")
        code = run_batch_order(str(self.db_path), ["b1", "b2"], str(output))
        self.assertEqual(code, 0)
        lines = parse_lines(output.read_text())
        self.assertEqual(lines[-1][0], "summary")
        leftovers = [
            p.name
            for p in self.db_path.parent.iterdir()
            if p.name.startswith(".order")
        ]
        self.assertEqual(leftovers, [])


class BatchOrderCliTests(BatchOrderDatabase):
    def test_cli_end_to_end(self) -> None:
        self.store_source("b1")
        self.store_derived("d", "b1", "h1")

        result = run_cli("batch-order", "--db", str(self.db_path), "d", "b1")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[0][:4], ["d", "derived", "b1", "b1"])
        self.assertEqual(lines[1][:4], ["b1", "source", "b1", None])
        self.assertEqual(lines[2][0], "summary")

    def test_cli_emits_fix_impact_and_fails_on_corrupt_snapshot(self) -> None:
        self.store_applied_derived("der")
        result = run_cli(
            "batch-order", "--db", str(self.db_path), "der", "src"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[0][0], "der")
        self.assertEqual([line[0] for line in lines],
                         ["der", "fix", "fix", "src", "summary"])
        self.assertEqual(lines[-1][1:4], [2, 1, 1])

        self.overwrite_snapshot("der", "{bad")
        result = run_cli(
            "batch-order", "--db", str(self.db_path), "der", "src"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("not valid JSON", result.stderr)

    def test_cli_requires_two_batch_ids(self) -> None:
        self.store_source("b1")
        result = run_cli("batch-order", "--db", str(self.db_path), "b1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("at least two", result.stderr)

    def test_cli_unknown_batch_and_missing_db_exit_nonzero(self) -> None:
        self.store_source("b1")
        result = run_cli("batch-order", "--db", str(self.db_path), "b1", "ghost")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ghost", result.stderr)
        self.assertEqual(result.stdout, "")

        result = run_cli(
            "batch-order",
            "--db",
            str(self.db_path.parent / "absent.db"),
            "a",
            "b",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_cli_writes_to_output_file(self) -> None:
        self.store_source("b1")
        self.store_source("b2")
        output = self.db_path.parent / "out.jsonl"
        result = run_cli(
            "batch-order",
            "--db",
            str(self.db_path),
            "b1",
            "b2",
            "--output",
            str(output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        lines = parse_lines(output.read_text())
        self.assertEqual(lines[-1][0], "summary")
        self.assertEqual(lines[-1][1:4], [2, 2, 0])


if __name__ == "__main__":
    unittest.main()
