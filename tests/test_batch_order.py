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
    build_correction_summary_rows,
    build_fix_impact_rows,
    run_batch_order,
)
from ops_workbench.orders_audit import AuditError, run_audit
from ops_workbench.decisions import run_decide
from ops_workbench.fixes import run_apply_fixes, run_propose_fix

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

    def update_snapshot(self, derived_id: str, snapshot: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE derived_batches SET snapshot_json = ? WHERE derived_id = ?",
                (snapshot, derived_id),
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


class BuildFixImpactRowsTests(unittest.TestCase):
    @staticmethod
    def _entry(identity, action, reason, triples):
        return (tuple(identity), action, reason, triples)

    def test_one_row_per_identity_sorted_ascending(self) -> None:
        entries = [
            self._entry(["invalid", 5, "sku"], "fix", "s", [[5, "sku", "S9"]]),
            self._entry(["duplicate", "A1", "S1"], "fix", "d", [[4, "sku", "S7"]]),
            self._entry(["invalid", 2, "qty"], "fix", "q", [[2, "qty", "5"]]),
        ]
        rows = build_fix_impact_rows("der", entries)
        self.assertEqual(
            [row[2] for row in rows],
            [
                ["duplicate", "A1", "S1"],
                ["invalid", 2, "qty"],
                ["invalid", 5, "sku"],
            ],
        )
        self.assertTrue(all(row[0] == "fix" and row[1] == "der" for row in rows))
        self.assertEqual(
            rows[1],
            ["fix", "der", ["invalid", 2, "qty"], ["fix", "q"], [[2, "qty", "5"]]],
        )

    def test_values_flatten_and_sort_by_record_then_field(self) -> None:
        # Stored triples deliberately out of order and covering two target
        # cells of one finding; every target cell is listed verbatim.
        entries = [
            self._entry(
                ["duplicate", "A1", "S1"],
                "fix",
                "d",
                [[4, "sku", "S7"], [3, "qty", "9"], [3, "sku", "S2"]],
            )
        ]
        rows = build_fix_impact_rows("der", entries)
        self.assertEqual(
            rows[0][4],
            [[3, "qty", "9"], [3, "sku", "S2"], [4, "sku", "S7"]],
        )

    def test_action_and_reason_are_echoed_from_snapshot(self) -> None:
        entries = [
            self._entry(["invalid", 2, "qty"], "fix", "  trimmed  ", [])
        ]
        rows = build_fix_impact_rows("der", entries)
        self.assertEqual(rows[0][3], ["fix", "  trimmed  "])

    def test_empty_entries_emit_no_rows(self) -> None:
        self.assertEqual(build_fix_impact_rows("der", []), [])


class BuildCorrectionSummaryRowsTests(unittest.TestCase):
    @staticmethod
    def _entry(identity, action, reason, triples):
        return (tuple(identity), action, reason, triples)

    def test_no_derived_batches_is_one_all_zero_chain_summary(self) -> None:
        self.assertEqual(
            build_correction_summary_rows([]),
            [["chain-summary", 0, 0, 0, 0]],
        )

    def test_empty_patches_contribute_no_steps(self) -> None:
        ordered = [
            ("d", [self._entry(["invalid", 2, "qty"], "fix", "r", [])]),
            ("e", []),
        ]
        self.assertEqual(
            build_correction_summary_rows(ordered),
            [["chain-summary", 0, 0, 0, 0]],
        )

    def test_global_sequence_orders_batches_identities_and_triples(self) -> None:
        ordered = [
            (
                "d1",
                [
                    # Entries and triples deliberately stored out of order.
                    self._entry(
                        ["invalid", 4, "qty"], "fix", "late",
                        [[4, "qty", "8"], [2, "qty", "5"]],
                    ),
                    self._entry(
                        ["duplicate", "A1", "S1"], "fix", "dup",
                        [[2, "qty", "7"]],
                    ),
                ],
            ),
            (
                "d2",
                [
                    self._entry(
                        ["invalid", 2, "qty"], "confirm", "keep",
                        [[2, "qty", "9"], [2, "status", "open"]],
                    ),
                ],
            ),
        ]
        rows = build_correction_summary_rows(ordered)
        # Cell (2, qty) sees, in execution order: the duplicate entry of d1
        # ("duplicate" sorts before "invalid"), the invalid entry of d1,
        # then d2.  The other cells see a single step.
        self.assertEqual(
            rows[0],
            [
                "cell", 2, "qty",
                [
                    ["d1", ["duplicate", "A1", "S1"], ["fix", "dup"], "7"],
                    ["d1", ["invalid", 4, "qty"], ["fix", "late"], "5"],
                    ["d2", ["invalid", 2, "qty"], ["confirm", "keep"], "9"],
                ],
                "9",
            ],
        )
        self.assertEqual(
            rows[1],
            [
                "cell", 2, "status",
                [["d2", ["invalid", 2, "qty"], ["confirm", "keep"], "open"]],
                "open",
            ],
        )
        self.assertEqual(
            rows[2],
            [
                "cell", 4, "qty",
                [["d1", ["invalid", 4, "qty"], ["fix", "late"], "8"]],
                "8",
            ],
        )
        # 3 corrected cells, 5 steps, one cell touched repeatedly; final
        # values originate from two distinct batches.
        self.assertEqual(rows[3], ["chain-summary", 3, 5, 1, 2])

    def test_repeated_cell_final_value_follows_execution_order(self) -> None:
        ordered = [
            ("a", [self._entry(["invalid", 2, "qty"], "fix", "r",
                               [[2, "qty", "first"]])]),
            ("b", [self._entry(["duplicate", "A1", "S1"], "fix", "r",
                               [[2, "qty", "second"]])]),
        ]
        rows = build_correction_summary_rows(ordered)
        self.assertEqual(rows[0][3][0][0], "a")
        self.assertEqual(rows[0][3][1][0], "b")
        self.assertEqual(rows[0][4], "second")
        self.assertEqual(rows[1], ["chain-summary", 1, 2, 1, 1])

        # Reversing the input order reverses execution: a now decides the
        # final value.
        rows = build_correction_summary_rows(list(reversed(ordered)))
        self.assertEqual(rows[0][3][0][0], "b")
        self.assertEqual(rows[0][4], "first")
        self.assertEqual(rows[1], ["chain-summary", 1, 2, 1, 1])


class FixImpactRunTests(BatchOrderDatabase):
    """Snapshot-driven fix rows through ``run_batch_order``."""

    @staticmethod
    def _snapshot_entry(identity, patch, action="fix", reason="r") -> dict:
        return {
            "identity": identity,
            "action": action,
            "reason": reason,
            "finding": identity,
            "proposal": {"finding": identity, "patch": patch},
        }

    def _snapshot(self, *entries: dict) -> str:
        return json.dumps(list(entries), ensure_ascii=False)

    def test_fix_rows_follow_their_derived_provenance_row(self) -> None:
        self.store_source("src")
        snapshot = self._snapshot(
            self._snapshot_entry(["invalid", 5, "sku"], [[5, "sku", "S9"]]),
            self._snapshot_entry(["duplicate", "A1", "S1"], [[4, "sku", "S7"]]),
            self._snapshot_entry(
                ["invalid", 2, "qty"], [[2, "qty", "5"]], reason="  q  "
            ),
        )
        self.store_derived("der", "src", "h1", snapshot)
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["der", "src"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        tags = [
            line[0]
            if line[0] in ("cell", "chain-summary", "fix", "summary")
            else line[1]
            for line in lines
        ]
        self.assertEqual(tags, [
            "cell", "cell", "cell", "chain-summary",
            "derived", "fix", "fix", "fix", "source", "summary",
        ])
        # Leading cross-batch summary: one cell row per corrected cell in
        # (record number, field) order, then the chain-level conclusion.
        self.assertEqual(
            lines[0],
            ["cell", 2, "qty",
             [["der", ["invalid", 2, "qty"], ["fix", "q"], "5"]], "5"],
        )
        self.assertEqual(
            lines[1],
            ["cell", 4, "sku",
             [["der", ["duplicate", "A1", "S1"], ["fix", "r"], "S7"]], "S7"],
        )
        self.assertEqual(
            lines[2],
            ["cell", 5, "sku",
             [["der", ["invalid", 5, "sku"], ["fix", "r"], "S9"]], "S9"],
        )
        self.assertEqual(lines[3], ["chain-summary", 3, 3, 0, 1])
        self.assertEqual(lines[4][:2], ["der", "derived"])
        self.assertEqual(
            lines[5],
            ["fix", "der", ["duplicate", "A1", "S1"], ["fix", "r"],
             [[4, "sku", "S7"]]],
        )
        self.assertEqual(
            lines[6],
            ["fix", "der", ["invalid", 2, "qty"], ["fix", "q"],
             [[2, "qty", "5"]]],
        )
        self.assertEqual(
            lines[7],
            ["fix", "der", ["invalid", 5, "sku"], ["fix", "r"],
             [[5, "sku", "S9"]]],
        )
        # The source batch emits no fix rows.
        self.assertEqual(lines[8][:2], ["src", "source"])
        # Summary counts only batches and never counts fix or summary rows.
        self.assertEqual(
            lines[9], ["summary", 2, 1, 1, sorted(["h1", lines[8][4]])]
        )

    def test_multiple_target_cells_are_all_listed_sorted(self) -> None:
        self.store_source("src")
        snapshot = self._snapshot(
            self._snapshot_entry(
                ["duplicate", "A1", "S1"],
                [[4, "sku", "S7"], [3, "qty", "9"], [3, "sku", "S2"]],
            )
        )
        self.store_derived("d1", "src", "h1", snapshot)
        self.store_derived("d2", "src", "h2", "[]")
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["d1", "d2"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        # Leading summary covers the three cells d1 touched; d2's empty
        # snapshot contributes no steps.
        self.assertEqual(
            [line[0] for line in lines[:5]],
            ["cell", "cell", "cell", "chain-summary", "d1"],
        )
        self.assertEqual(
            lines[0],
            ["cell", 3, "qty",
             [["d1", ["duplicate", "A1", "S1"], ["fix", "r"], "9"]], "9"],
        )
        self.assertEqual(
            lines[1],
            ["cell", 3, "sku",
             [["d1", ["duplicate", "A1", "S1"], ["fix", "r"], "S2"]], "S2"],
        )
        self.assertEqual(
            lines[2],
            ["cell", 4, "sku",
             [["d1", ["duplicate", "A1", "S1"], ["fix", "r"], "S7"]], "S7"],
        )
        self.assertEqual(lines[3], ["chain-summary", 3, 3, 0, 1])
        self.assertEqual(lines[4][1], "derived")
        self.assertEqual(
            lines[5],
            ["fix", "d1", ["duplicate", "A1", "S1"], ["fix", "r"],
             [[3, "qty", "9"], [3, "sku", "S2"], [4, "sku", "S7"]]],
        )
        # Empty snapshot: provenance row only, no fix rows before summary.
        self.assertEqual(lines[6][:2], ["d2", "derived"])
        self.assertEqual(lines[7], ["summary", 2, 0, 2, ["h1", "h2"]])

    def test_multiple_derived_batches_order_by_input_then_identity(self) -> None:
        self.store_source("src")
        snap_b = self._snapshot(
            self._snapshot_entry(["invalid", 2, "qty"], [[2, "qty", "5"]])
        )
        snap_a = self._snapshot(
            self._snapshot_entry(["invalid", 3, "qty"], [[3, "qty", "6"]])
        )
        self.store_derived("b", "src", "hb", snap_b)
        self.store_derived("a", "src", "ha", snap_a)
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["b", "a"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        tags = [
            line[0]
            if line[0] in ("cell", "chain-summary", "fix", "summary")
            else line[1]
            for line in lines
        ]
        self.assertEqual(tags, [
            "cell", "cell", "chain-summary",
            "derived", "fix", "derived", "fix", "summary",
        ])
        # The global sequence follows batch input order: b before a.
        self.assertEqual(
            lines[0],
            ["cell", 2, "qty",
             [["b", ["invalid", 2, "qty"], ["fix", "r"], "5"]], "5"],
        )
        self.assertEqual(
            lines[1],
            ["cell", 3, "qty",
             [["a", ["invalid", 3, "qty"], ["fix", "r"], "6"]], "6"],
        )
        self.assertEqual(lines[2], ["chain-summary", 2, 2, 0, 2])
        self.assertEqual([lines[4][1], lines[4][2]], ["b", ["invalid", 2, "qty"]])
        self.assertEqual([lines[6][1], lines[6][2]], ["a", ["invalid", 3, "qty"]])
        self.assertEqual(lines[7][0], "summary")

    def test_snapshot_is_parsed_once_for_a_repeated_batch_id(self) -> None:
        self.store_source("src")
        snapshot = self._snapshot(
            self._snapshot_entry(["invalid", 2, "qty"], [[2, "qty", "5"]])
        )
        self.store_derived("der", "src", "h1", snapshot)
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["der", "der"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        tags = [
            line[0]
            if line[0] in ("cell", "chain-summary", "fix", "summary")
            else line[1]
            for line in lines
        ]
        # The repeated batch id explains its provenance twice, but its
        # corrections feed the global sequence only once.
        self.assertEqual(tags, [
            "cell", "chain-summary",
            "derived", "fix", "derived", "fix", "summary",
        ])
        self.assertEqual(
            lines[0],
            ["cell", 2, "qty",
             [["der", ["invalid", 2, "qty"], ["fix", "r"], "5"]], "5"],
        )
        self.assertEqual(lines[1], ["chain-summary", 1, 1, 0, 1])
        self.assertEqual(lines[-1], ["summary", 2, 0, 2, ["h1"]])

    def test_corrupt_snapshot_is_fatal_without_partial_output(self) -> None:
        self.store_source("src")
        self.store_derived("der", "src", "h1", "{not json")
        out = io.BytesIO()
        with self.assertRaises(AuditError) as ctx:
            run_batch_order(str(self.db_path), ["der", "src"], stdout=out)
        self.assertIn("snapshot", ctx.exception.message)
        self.assertEqual(out.getvalue(), b"")

        output = self.db_path.parent / "order.jsonl"
        output.write_text("OLD CONTENT")
        with self.assertRaises(AuditError):
            run_batch_order(str(self.db_path), ["der", "src"], str(output))
        self.assertEqual(output.read_text(), "OLD CONTENT")

    def test_incomplete_snapshot_entries_are_fatal(self) -> None:
        self.store_source("src")
        self.store_derived("der", "src", "h1", "[]")
        good = self._snapshot_entry(["invalid", 2, "qty"], [[2, "qty", "5"]])
        bad_snapshots = [
            "{}",  # not an array
            json.dumps([{}]),  # missing identity/decision/proposal
            json.dumps([{"action": "fix", "reason": "r",
                         "proposal": {"patch": []}}]),  # missing identity
            json.dumps([{"identity": ["invalid", 2, "qty"], "reason": "r",
                         "proposal": {"patch": []}}]),  # missing action
            json.dumps([{"identity": ["invalid", 2, "qty"], "action": "fix",
                         "reason": "r"}]),  # missing proposal
            json.dumps([{"identity": ["bogus", 2, "qty"], "action": "fix",
                         "reason": "r",
                         "proposal": {"patch": []}}]),  # bad identity kind
            json.dumps([dict(good, proposal={"finding": good["finding"]})]),
            json.dumps([dict(good, proposal={"patch": "[]"})]),
            json.dumps([dict(good, proposal={"patch": [[2, "qty", 5]]})]),
        ]
        for snapshot in bad_snapshots:
            with self.subTest(snapshot=snapshot):
                self.update_snapshot("der", snapshot)
                out = io.BytesIO()
                with self.assertRaises(AuditError):
                    run_batch_order(
                        str(self.db_path), ["src", "der"], stdout=out
                    )
                self.assertEqual(out.getvalue(), b"")

    def test_a_corrupt_snapshot_anywhere_fails_the_whole_run(self) -> None:
        self.store_source("src")
        good = self._snapshot(
            self._snapshot_entry(["invalid", 2, "qty"], [[2, "qty", "5"]])
        )
        self.store_derived("good", "src", "hg", good)
        self.store_derived("bad", "src", "hb", "[]")
        self.update_snapshot("bad", "[")
        out = io.BytesIO()
        # The good batch appears first, but nothing may be emitted.
        with self.assertRaises(AuditError):
            run_batch_order(str(self.db_path), ["good", "bad"], stdout=out)
        self.assertEqual(out.getvalue(), b"")

    def test_repeated_cell_across_batches_lists_every_step_and_winner(self) -> None:
        self.store_source("src")
        snap_a = self._snapshot(
            self._snapshot_entry(
                ["duplicate", "A1", "S1"],
                [[2, "qty", "5"], [2, "status", "open"]],
                reason="  first  ",
            )
        )
        snap_b = self._snapshot(
            self._snapshot_entry(["invalid", 2, "qty"], [[2, "qty", "9"]])
        )
        self.store_derived("a", "src", "ha", snap_a)
        self.store_derived("b", "src", "hb", snap_b)
        out = io.BytesIO()
        code = run_batch_order(
            str(self.db_path), ["src", "a", "b"], stdout=out
        )
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        # Two corrected cells; (2, qty) is rewritten by both batches.
        self.assertEqual(
            lines[0],
            [
                "cell", 2, "qty",
                [
                    ["a", ["duplicate", "A1", "S1"], ["fix", "first"], "5"],
                    ["b", ["invalid", 2, "qty"], ["fix", "r"], "9"],
                ],
                "9",
            ],
        )
        self.assertEqual(
            lines[1],
            [
                "cell", 2, "status",
                [["a", ["duplicate", "A1", "S1"], ["fix", "first"], "open"]],
                "open",
            ],
        )
        # 2 cells, 3 steps, 1 repeated, both batches own a final value.
        self.assertEqual(lines[2], ["chain-summary", 2, 3, 1, 2])

    def test_summary_leads_even_when_source_batch_is_first_input(self) -> None:
        self.store_source("src")
        snapshot = self._snapshot(
            self._snapshot_entry(["invalid", 2, "qty"], [[2, "qty", "5"]])
        )
        self.store_derived("der", "src", "h1", snapshot)
        out = io.BytesIO()
        code = run_batch_order(
            str(self.db_path), ["src", "der"], stdout=out
        )
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(lines[0][0], "cell")
        self.assertEqual(lines[1], ["chain-summary", 1, 1, 0, 1])
        self.assertEqual(lines[2][:2], ["src", "source"])
        self.assertEqual(lines[-1], ["summary", 2, 1, 1, lines[-1][4]])


class FixImpactEndToEndTests(BatchOrderDatabase):
    SCHEMA = json.dumps(
        {
            "order_id": ["oid"],
            "sku": ["sku"],
            "qty": ["qty"],
            "status": ["status"],
            "updated_at": ["updated_at"],
        }
    )
    TS = "2024-01-02T03:04:05Z"
    CSV = (
        "oid,sku,qty,status,updated_at\n"
        + f"A0,S0,0,open,{TS}\n"
        + f"A1,S1,2,open,{TS}\n"
        + f"A1,S1,2,open,{TS}\n"
    )

    def _build_derived(self) -> str:
        self.input.write_text(self.CSV)
        code = run_audit(
            self.SCHEMA, str(self.input), stdout=io.BytesIO(),
            db_path=str(self.db_path), batch_id="src",
        )
        self.assertEqual(code, 1)
        run_decide(
            str(self.db_path), "src", '["invalid",2,"qty"]', "fix", "  q  "
        )
        run_decide(
            str(self.db_path), "src", '["duplicate","A1","S1"]', "fix", "d"
        )
        run_propose_fix(
            str(self.db_path), "src", '["invalid",2,"qty"]', '[[2,"qty","5"]]'
        )
        run_propose_fix(
            str(self.db_path), "src", '["duplicate","A1","S1"]',
            '[[3,"sku","S2"]]',
        )
        fixed = self.db_path.parent / "fixed.csv"
        self.assertEqual(
            run_apply_fixes(
                str(self.db_path), "src", "der", str(self.input), str(fixed)
            ),
            0,
        )
        return hashlib.sha256(fixed.read_bytes()).hexdigest()

    def test_explains_real_apply_fixes_snapshot(self) -> None:
        digest = self._build_derived()
        out = io.BytesIO()
        code = run_batch_order(
            str(self.db_path), ["src", "der"], stdout=out
        )
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(len(lines), 8)
        # Cross-batch summary leads even though src is the first batch.
        self.assertEqual(
            lines[0],
            ["cell", 2, "qty",
             [["der", ["invalid", 2, "qty"], ["fix", "q"], "5"]], "5"],
        )
        self.assertEqual(
            lines[1],
            ["cell", 3, "sku",
             [["der", ["duplicate", "A1", "S1"], ["fix", "d"], "S2"]], "S2"],
        )
        self.assertEqual(lines[2], ["chain-summary", 2, 2, 0, 1])
        self.assertEqual(lines[3][:2], ["src", "source"])
        self.assertEqual(lines[4], ["der", "derived", "src", "src", digest])
        self.assertEqual(
            lines[5],
            ["fix", "der", ["duplicate", "A1", "S1"], ["fix", "d"],
             [[3, "sku", "S2"]]],
        )
        self.assertEqual(
            lines[6],
            ["fix", "der", ["invalid", 2, "qty"], ["fix", "q"],
             [[2, "qty", "5"]]],
        )
        self.assertEqual(lines[7][0], "summary")
        self.assertEqual(lines[7][1:4], [2, 1, 1])


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
                ["chain-summary", 0, 0, 0, 0],
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
        self.assertEqual(lines[0], ["chain-summary", 0, 0, 0, 0])
        self.assertEqual(lines[1], ["d2", "derived", None, "d1", "h2"])
        self.assertEqual(lines[2][1], "source")
        self.assertEqual(lines[3], ["summary", 2, 1, 1, sorted({"h2", lines[2][4]})])

    def test_direct_source_that_never_existed_has_null_source(self) -> None:
        self.store_derived("d", "ghost", "h1")
        out = io.BytesIO()
        code = run_batch_order(str(self.db_path), ["d"], stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue())
        self.assertEqual(lines[0], ["chain-summary", 0, 0, 0, 0])
        self.assertEqual(lines[1], ["d", "derived", None, "ghost", "h1"])

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
        # d carries an empty snapshot, so only the all-zero chain summary
        # precedes the provenance rows.
        self.assertEqual(lines[0], ["chain-summary", 0, 0, 0, 0])
        self.assertEqual(lines[1][:4], ["d", "derived", "b1", "b1"])
        self.assertEqual(lines[2][:4], ["b1", "source", "b1", None])
        self.assertEqual(lines[3][0], "summary")

    def test_cli_emits_fix_rows_from_derived_snapshot(self) -> None:
        self.store_source("b1")
        snapshot = json.dumps([
            {
                "identity": ["duplicate", "A1", "S1"],
                "action": "fix",
                "reason": "d",
                "finding": ["duplicate", ["A1", "S1"], [3, 4]],
                "proposal": {
                    "finding": ["duplicate", ["A1", "S1"], [3, 4]],
                    "patch": [[4, "sku", "S7"], [3, "qty", "9"]],
                },
            },
        ])
        self.store_derived("d", "b1", "h1", snapshot)
        result = run_cli("batch-order", "--db", str(self.db_path), "d", "b1")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(
            lines[0],
            ["cell", 3, "qty",
             [["d", ["duplicate", "A1", "S1"], ["fix", "d"], "9"]], "9"],
        )
        self.assertEqual(
            lines[1],
            ["cell", 4, "sku",
             [["d", ["duplicate", "A1", "S1"], ["fix", "d"], "S7"]], "S7"],
        )
        self.assertEqual(lines[2], ["chain-summary", 2, 2, 0, 1])
        self.assertEqual(lines[3][:2], ["d", "derived"])
        self.assertEqual(
            lines[4],
            ["fix", "d", ["duplicate", "A1", "S1"], ["fix", "d"],
             [[3, "qty", "9"], [4, "sku", "S7"]]],
        )
        self.assertEqual(lines[5][:2], ["b1", "source"])
        self.assertEqual(
            lines[6], ["summary", 2, 1, 1, sorted(["h1", lines[5][4]])]
        )

    def test_cli_corrupt_snapshot_exits_two_with_empty_stdout(self) -> None:
        self.store_source("b1")
        self.store_derived("d", "b1", "h1", "{broken")
        result = run_cli("batch-order", "--db", str(self.db_path), "d", "b1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("snapshot", result.stderr)

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
