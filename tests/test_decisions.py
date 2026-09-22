"""Checks for manual decisions (``decide``) and ``review-decisions``."""

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.decisions import (
    DecisionConflictError,
    build_review,
    run_decide,
    run_review,
)
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
# Findings: invalid qty at record 2, empty sku at record 3, bad timestamp at 4.
OLD_CSV = (
    HEADER
    + "A1,S1,0,open,2024-01-02T03:04:05Z,x\n"
    + "A2,,3,open,2024-01-02T03:04:05Z,x\n"
    + "A3,S3,1,open,notadate,x\n"
)
# Record 2 keeps its identity with a different value (changed); record 3 is
# still there; record 4 now parses (resolved).
NEW_CSV = (
    HEADER
    + "A1,S1,-5,open,2024-01-02T03:04:05Z,x\n"
    + "A2,,3,open,2024-01-02T03:04:05Z,x\n"
    + "A3,S3,1,open,2024-01-02T03:04:06Z,x\n"
)
QTY_ID = '["invalid",2,"qty"]'
TS_ID = '["invalid",4,"updated_at"]'


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


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.input.write_bytes(OLD_CSV.encode())
        self.db = str(base / "batches.db")
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id="b1")
        self.assertEqual(code, 1)

    def decisions(self) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT batch_id, identity_json, action, reason, finding_json "
                "FROM decisions ORDER BY identity_json"
            ).fetchall()
        finally:
            conn.close()

    def test_stores_decision_with_full_finding_and_trims_reason(self) -> None:
        code = run_decide(self.db, "b1", QTY_ID, "confirm", "  real issue  ")
        self.assertEqual(code, 0)
        rows = self.decisions()
        self.assertEqual(len(rows), 1)
        batch_id, identity_json, action, reason, finding_json = rows[0]
        self.assertEqual(batch_id, "b1")
        self.assertEqual(json.loads(identity_json), ["invalid", 2, "qty"])
        self.assertEqual(action, "confirm")
        self.assertEqual(reason, "real issue")
        self.assertEqual(
            json.loads(finding_json), ["invalid", 2, "qty", "0"]
        )

    def test_identity_json_accepts_whitespace_and_canonicalizes(self) -> None:
        code = run_decide(self.db, "b1", '["invalid", 2, "qty"]', "fix", "x")
        self.assertEqual(code, 0)
        self.assertEqual(
            self.decisions()[0][1], '["invalid",2,"qty"]'
        )

    def test_repeat_same_decision_is_idempotent(self) -> None:
        for _ in range(2):
            code = run_decide(self.db, "b1", QTY_ID, "confirm", "real issue")
            self.assertEqual(code, 0)
        rows = self.decisions()
        self.assertEqual(len(rows), 1)

        # Repeating with surrounding whitespace in the reason is the same
        # trimmed content and therefore also idempotent.
        code = run_decide(self.db, "b1", QTY_ID, "confirm", "  real issue ")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.decisions()), 1)

    def test_same_identity_different_content_exits_three_changes_nothing(self) -> None:
        self.assertEqual(run_decide(self.db, "b1", QTY_ID, "confirm", "a"), 0)
        before = self.decisions()
        for action, reason in [("ignore", "a"), ("confirm", "b")]:
            with self.subTest(action=action, reason=reason):
                with self.assertRaises(DecisionConflictError) as ctx:
                    run_decide(self.db, "b1", QTY_ID, action, reason)
                self.assertEqual(ctx.exception.batch_id, "b1")
                self.assertEqual(ctx.exception.identity, ["invalid", 2, "qty"])
                self.assertEqual(self.decisions(), before)

    def test_distinct_identities_coexist(self) -> None:
        self.assertEqual(run_decide(self.db, "b1", QTY_ID, "confirm", "a"), 0)
        self.assertEqual(run_decide(self.db, "b1", TS_ID, "ignore", "b"), 0)
        self.assertEqual(len(self.decisions()), 2)

    def test_bad_parameters_exit_two_without_writing(self) -> None:
        cases = [
            ("b1", QTY_ID, "maybe", "r", "ACTION"),
            ("b1", QTY_ID, "confirm", "   ", "REASON"),
            ("b1", QTY_ID, "confirm", "", "REASON"),
            ("b1", "not json", "confirm", "r", "ID"),
            ("b1", '["invalid",99,"qty"]', "confirm", "r", "ID"),
            ("b1", '["duplicate","A1","S1"]', "confirm", "r", "ID"),
            ("ghost", QTY_ID, "confirm", "r", self.db),
        ]
        for batch_id, ident, action, reason, fragment in cases:
            with self.subTest(batch_id=batch_id, ident=ident, action=action):
                with self.assertRaises(AuditError) as ctx:
                    run_decide(self.db, batch_id, ident, action, reason)
                self.assertIn(fragment, f"{ctx.exception.filename} "
                                        f"{ctx.exception.message}")
        # Every failure happens before the write transaction opens, so the
        # decisions table must not have been created at all.
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("SELECT COUNT(*) FROM decisions").fetchone()
        finally:
            conn.close()

    def test_missing_database_is_fatal(self) -> None:
        missing = str(Path(self.dir.name) / "absent.db")
        with self.assertRaises(AuditError):
            run_decide(missing, "b1", QTY_ID, "confirm", "r")
        self.assertFalse(Path(missing).exists())

    def test_cli_exit_codes(self) -> None:
        ok = run_cli("decide", "--db", self.db, "b1", QTY_ID, "confirm", "note")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout, "")

        again = run_cli("decide", "--db", self.db, "b1", QTY_ID,
                        "confirm", "note")
        self.assertEqual(again.returncode, 0, again.stderr)

        conflict = run_cli("decide", "--db", self.db, "b1", QTY_ID,
                           "ignore", "note")
        self.assertEqual(conflict.returncode, 3)
        self.assertIn("b1", conflict.stderr)
        self.assertEqual(conflict.stdout, "")

        missing = run_cli("decide", "--db", self.db, "ghost", QTY_ID,
                          "confirm", "note")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("ghost", missing.stderr)

        bad_action = run_cli("decide", "--db", self.db, "b1", QTY_ID,
                             "maybe", "note")
        self.assertEqual(bad_action.returncode, 2)
        self.assertIn("maybe", bad_action.stderr)

        blank_reason = run_cli("decide", "--db", self.db, "b1", TS_ID,
                               "fix", "   ")
        self.assertEqual(blank_reason.returncode, 2)
        self.assertIn("REASON", blank_reason.stderr)


class ReviewDecisionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.db = str(base / "batches.db")
        self.old_hash = self.store("old", OLD_CSV)
        self.new_hash = self.store("new", NEW_CSV)

    def store(self, batch_id: str, csv_text: str) -> str:
        self.input.write_bytes(csv_text.encode())
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id=batch_id)
        self.assertIn(code, (0, 1))
        return hashlib.sha256(csv_text.encode()).hexdigest()

    def test_kept_changed_resolved_and_pending(self) -> None:
        self.assertEqual(
            run_decide(self.db, "old", QTY_ID, "confirm", "real issue"), 0
        )
        self.assertEqual(
            run_decide(self.db, "old", TS_ID, "ignore", "obsolete"), 0
        )
        out = io.BytesIO()
        code = run_review(self.db, "old", "new", stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(
            parse_lines(out.getvalue()),
            [
                ["invalid", ["invalid", 2, "qty"], "changed",
                 ["confirm", "real issue"],
                 ["invalid", 2, "qty", "0"],
                 ["invalid", 2, "qty", "-5"]],
                ["pending", ["invalid", 2, "qty"],
                 ["invalid", 2, "qty", "-5"]],
                ["pending", ["invalid", 3, "sku"],
                 ["invalid", 3, "sku", ""]],
                ["invalid", ["invalid", 4, "updated_at"], "resolved",
                 ["ignore", "obsolete"],
                 ["invalid", 4, "updated_at", "notadate"], None],
                ["summary", 0, 2, 2, self.old_hash, self.new_hash],
            ],
        )

    def test_unchanged_findings_are_kept_undecided_still_pending(self) -> None:
        self.assertEqual(
            run_decide(self.db, "old", QTY_ID, "fix", "soon"), 0
        )
        self.input.write_bytes(OLD_CSV.encode())
        same_hash = self.store("new2", OLD_CSV)
        out = io.BytesIO()
        self.assertEqual(run_review(self.db, "old", "new2", stdout=out), 0)
        self.assertEqual(
            parse_lines(out.getvalue()),
            [
                ["kept", ["invalid", 2, "qty"], ["fix", "soon"],
                 ["invalid", 2, "qty", "0"]],
                ["pending", ["invalid", 3, "sku"],
                 ["invalid", 3, "sku", ""]],
                ["pending", ["invalid", 4, "updated_at"],
                 ["invalid", 4, "updated_at", "notadate"]],
                ["summary", 1, 0, 2, self.old_hash, same_hash],
            ],
        )

    def test_group_finding_change_is_reviewed_by_group_identity(self) -> None:
        dup_old = (
            HEADER
            + "A1,S1,1,open,2024-01-02T03:04:05Z,x\n"
            + "A1,S1,1,open,2024-01-02T03:04:05Z,x\n"
        )
        dup_new = dup_old + "A1,S1,1,open,2024-01-02T03:04:05Z,x\n"
        old_hash = self.store("d-old", dup_old)
        new_hash = self.store("d-new", dup_new)
        self.assertEqual(
            run_decide(self.db, "d-old", '["duplicate","A1","S1"]',
                       "confirm", "known dup"),
            0,
        )
        out = io.BytesIO()
        self.assertEqual(run_review(self.db, "d-old", "d-new", stdout=out), 0)
        self.assertEqual(
            parse_lines(out.getvalue()),
            [
                ["invalid", ["duplicate", "A1", "S1"], "changed",
                 ["confirm", "known dup"],
                 ["duplicate", ["A1", "S1"], [2, 3]],
                 ["duplicate", ["A1", "S1"], [2, 3, 4]]],
                ["pending", ["duplicate", "A1", "S1"],
                 ["duplicate", ["A1", "S1"], [2, 3, 4]]],
                ["summary", 0, 1, 1, old_hash, new_hash],
            ],
        )

    def test_blocks_order_by_identity_invalid_before_pending(self) -> None:
        # Direct unit check: a pending-only identity must sort among the
        # decision blocks, not after them.
        old_findings = [["invalid", 5, "qty", "0"]]
        old_decisions = [
            ('["invalid",5,"qty"]', "confirm", "r",
             '["invalid",5,"qty","0"]'),
        ]
        new_findings = [
            ["invalid", 2, "sku", ""],
            ["invalid", 5, "qty", "1"],
        ]
        items = build_review(old_findings, old_decisions, new_findings)
        self.assertEqual(
            [item[0:2] for item in items],
            [
                ["pending", ["invalid", 2, "sku"]],
                ["invalid", ["invalid", 5, "qty"]],
                ["pending", ["invalid", 5, "qty"]],
            ],
        )

    def test_reasons_are_emitted_with_their_stored_text(self) -> None:
        run_decide(self.db, "old", QTY_ID, "confirm", "  spaced  ")
        out = io.BytesIO()
        run_review(self.db, "old", "new", stdout=out)
        first = parse_lines(out.getvalue())[0]
        self.assertEqual(first[3], ["confirm", "spaced"])

    def test_missing_batch_or_database_is_fatal_without_partial_results(self) -> None:
        out = io.BytesIO()
        with self.assertRaises(AuditError) as ctx:
            run_review(self.db, "ghost", "new", stdout=out)
        self.assertIn("ghost", ctx.exception.message)
        self.assertEqual(out.getvalue(), b"")

        output = Path(self.dir.name) / "review.jsonl"
        output.write_text("OLD CONTENT")
        with self.assertRaises(AuditError):
            run_review(self.db, "old", "ghost", str(output))
        self.assertEqual(output.read_text(), "OLD CONTENT")

        with self.assertRaises(AuditError):
            run_review(str(Path(self.dir.name) / "absent.db"), "old", "new",
                       stdout=io.BytesIO())

    def test_output_is_written_atomically(self) -> None:
        run_decide(self.db, "old", QTY_ID, "confirm", "real issue")
        output = Path(self.dir.name) / "review.jsonl"
        output.write_text("OLD CONTENT")
        code = run_review(self.db, "old", "new", str(output))
        self.assertEqual(code, 0)
        lines = parse_lines(output.read_text())
        # Only the changed qty finding carries a decision here; the resolved
        # timestamp finding had none and therefore produces no row.
        self.assertEqual(lines[-1],
                         ["summary", 0, 1, 2, self.old_hash, self.new_hash])
        leftovers = [
            p.name for p in Path(self.dir.name).iterdir()
            if p.name.startswith(".review")
        ]
        self.assertEqual(leftovers, [])

    def test_cli_end_to_end(self) -> None:
        decided = run_cli("decide", "--db", self.db, "old", QTY_ID,
                          "confirm", "real issue")
        self.assertEqual(decided.returncode, 0, decided.stderr)

        result = run_cli("review-decisions", "--db", self.db, "old", "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[0][0], "invalid")
        self.assertEqual(lines[0][2], "changed")
        self.assertEqual(lines[1][0], "pending")
        self.assertEqual(lines[-1][0], "summary")
        self.assertEqual(lines[-1][1:4], [0, 1, 2])

        output = Path(self.dir.name) / "out.jsonl"
        to_file = run_cli("review-decisions", "--db", self.db, "old", "new",
                          "--output", str(output))
        self.assertEqual(to_file.returncode, 0, to_file.stderr)
        self.assertEqual(to_file.stdout, "")
        self.assertEqual(parse_lines(output.read_text())[-1][0], "summary")

        missing = run_cli("review-decisions", "--db", self.db, "old", "ghost")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("ghost", missing.stderr)
        self.assertEqual(missing.stdout, "")


if __name__ == "__main__":
    unittest.main()
