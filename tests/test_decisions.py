"""Checks for the ``decide`` and ``review-decisions`` subcommands."""

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
    review_decisions,
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
# Record 2 has a bad qty (invalid finding); records 3 and 4 are a duplicate
# group whose quantities differ (duplicate + conflict findings).
OLD_CSV = (
    HEADER
    + "A1,S1,0,open,2024-01-02T03:04:05Z,x\n"
    + "A2,S2,3,open,2024-01-02T03:04:05Z,x\n"
    + "A2,S2,4,open,2024-01-02T03:04:05Z,x\n"
)
# A blank record 3 shifts the group to records 4/5, changing the full
# duplicate/conflict findings while keeping their identities.
CHANGED_CSV = (
    HEADER
    + "A1,S1,0,open,2024-01-02T03:04:05Z,x\n"
    + "\n"
    + "A2,S2,3,open,2024-01-02T03:04:05Z,x\n"
    + "A2,S2,4,open,2024-01-02T03:04:05Z,x\n"
)
# Valid qty and a single A2 row: every OLD finding is gone.
RESOLVED_CSV = (
    HEADER
    + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
    + "A2,S2,3,open,2024-01-02T03:04:05Z,x\n"
)

INVALID_ID = '["invalid",2,"qty"]'
DUP_ID = '["duplicate","A2","S2"]'
CONFLICT_ID = '["conflict","A2","S2"]'

INVALID_FINDING = ["invalid", 2, "qty", "0"]
DUP_FINDING = ["duplicate", ["A2", "S2"], [3, 4]]
CONFLICT_FINDING = ["conflict", ["A2", "S2"], [3, 4]]
DUP_FINDING_CHANGED = ["duplicate", ["A2", "S2"], [4, 5]]


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


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.db = str(base / "batches.db")
        self.input.write_bytes(OLD_CSV.encode())
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id="old")
        self.assertEqual(code, 1)

    def stored_decisions(self, batch_id: str = "old") -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            try:
                return conn.execute(
                    "SELECT batch_id, identity_json, action, reason, finding_json "
                    "FROM decisions WHERE batch_id = ? ORDER BY identity_json",
                    (batch_id,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        finally:
            conn.close()

    def test_decision_stores_action_trimmed_reason_and_full_finding(self) -> None:
        code = run_decide(self.db, "old", INVALID_ID, "confirm", "  true positive ")
        self.assertEqual(code, 0)
        rows = self.stored_decisions()
        self.assertEqual(len(rows), 1)
        batch_id, identity_json, action, reason, finding_json = rows[0]
        self.assertEqual(batch_id, "old")
        self.assertEqual(json.loads(identity_json), ["invalid", 2, "qty"])
        self.assertEqual(action, "confirm")
        self.assertEqual(reason, "true positive")
        self.assertEqual(json.loads(finding_json), INVALID_FINDING)

    def test_group_finding_identity_hits(self) -> None:
        self.assertEqual(
            run_decide(self.db, "old", DUP_ID, "fix", "merge rows"), 0
        )
        self.assertEqual(
            run_decide(self.db, "old", CONFLICT_ID, "ignore", "noise"), 0
        )
        rows = {r[1]: r for r in self.stored_decisions()}
        self.assertEqual(
            json.loads(rows[DUP_ID][4]), DUP_FINDING
        )
        self.assertEqual(
            json.loads(rows[CONFLICT_ID][4]), CONFLICT_FINDING
        )

    def test_identical_decision_is_idempotent(self) -> None:
        self.assertEqual(
            run_decide(self.db, "old", INVALID_ID, "confirm", "same"), 0
        )
        # Surrounding whitespace trims to the same stored content.
        self.assertEqual(
            run_decide(self.db, "old", INVALID_ID, "confirm", "  same\t"), 0
        )
        rows = self.stored_decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "same")

    def test_same_identity_different_content_exits_three(self) -> None:
        self.assertEqual(
            run_decide(self.db, "old", INVALID_ID, "confirm", "first"), 0
        )
        with self.assertRaises(DecisionConflictError) as ctx:
            run_decide(self.db, "old", INVALID_ID, "ignore", "first")
        self.assertEqual(ctx.exception.batch_id, "old")
        self.assertEqual(ctx.exception.identity, ["invalid", 2, "qty"])

        with self.assertRaises(DecisionConflictError):
            run_decide(self.db, "old", INVALID_ID, "confirm", "second")

        # The original decision is untouched.
        rows = self.stored_decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][2], rows[0][3]), ("confirm", "first"))

    def test_bad_arguments_exit_two_without_writing(self) -> None:
        bad_id_cases = [
            ("not json", "not json", "confirm", "r"),
            ("short array", '["invalid",2]', "confirm", "r"),
            ("object", '{"a":1}', "confirm", "r"),
        ]
        for label, identity, action, reason in bad_id_cases:
            with self.subTest(label):
                with self.assertRaises(AuditError):
                    run_decide(self.db, "old", identity, action, reason)

        with self.assertRaises(AuditError):
            run_decide(self.db, "old", INVALID_ID, "resolve", "r")
        for blank in ("", "   ", "\t\n"):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(AuditError):
                    run_decide(self.db, "old", INVALID_ID, "confirm", blank)

        # Identity that matches no finding of the batch.
        with self.assertRaises(AuditError) as ctx:
            run_decide(self.db, "old", '["invalid",9,"sku"]', "confirm", "r")
        self.assertIn("does not match", ctx.exception.message)

        self.assertEqual(self.stored_decisions(), [])

    def test_missing_batch_or_database_is_fatal(self) -> None:
        with self.assertRaises(AuditError):
            run_decide(self.db, "ghost", INVALID_ID, "confirm", "r")
        with self.assertRaises(AuditError):
            run_decide(
                str(Path(self.dir.name) / "absent.db"),
                "old", INVALID_ID, "confirm", "r",
            )

    def test_cli_exit_codes(self) -> None:
        ok = run_cli("decide", "--db", self.db, "old", INVALID_ID,
                     "confirm", "kept")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout, "")

        again = run_cli("decide", "--db", self.db, "old", INVALID_ID,
                        "confirm", "kept")
        self.assertEqual(again.returncode, 0, again.stderr)

        conflict = run_cli("decide", "--db", self.db, "old", INVALID_ID,
                           "fix", "different")
        self.assertEqual(conflict.returncode, 3)
        self.assertIn("already stored", conflict.stderr)

        bogus = run_cli("decide", "--db", self.db, "old",
                        '["nope"]', "confirm", "r")
        self.assertEqual(bogus.returncode, 2)
        self.assertEqual(bogus.stdout, "")

        bad_action = run_cli("decide", "--db", self.db, "old",
                             INVALID_ID, "resolve", "r")
        self.assertEqual(bad_action.returncode, 2)

        missing = run_cli("decide", "--db", self.db, "ghost",
                          INVALID_ID, "confirm", "r")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("ghost", missing.stderr)


class ReviewDecisionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.db = str(base / "batches.db")

    def store(self, batch_id: str, csv_text: str) -> str:
        self.input.write_bytes(csv_text.encode())
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id=batch_id)
        self.assertIn(code, (0, 1))
        return hashlib.sha256(csv_text.encode()).hexdigest()

    def seed_decisions(self) -> None:
        # Decisions for the invalid finding and the duplicate finding; the
        # conflict finding is left undecided.
        self.assertEqual(
            run_decide(self.db, "old", INVALID_ID, "confirm", "r1"), 0
        )
        self.assertEqual(
            run_decide(self.db, "old", DUP_ID, "ignore", "r2"), 0
        )

    def review(self, old: str = "old", new: str = "new") -> list[list]:
        out = io.BytesIO()
        code = run_review(self.db, old, new, stdout=out)
        self.assertEqual(code, 0)
        return parse_lines(out.getvalue().decode())

    def test_unchanged_findings_are_kept_undecided_are_pending(self) -> None:
        old_hash = self.store("old", OLD_CSV)
        new_hash = self.store("new", OLD_CSV)
        self.seed_decisions()

        lines = self.review()
        self.assertEqual(
            lines,
            [
                ["pending", ["conflict", "A2", "S2"], CONFLICT_FINDING],
                ["kept", ["duplicate", "A2", "S2"], ["ignore", "r2"],
                 DUP_FINDING],
                ["kept", ["invalid", 2, "qty"], ["confirm", "r1"],
                 INVALID_FINDING],
                ["summary", 2, 0, 1, old_hash, new_hash],
            ],
        )

    def test_changed_finding_is_invalid_then_pending(self) -> None:
        old_hash = self.store("old", OLD_CSV)
        new_hash = self.store("new", CHANGED_CSV)
        self.seed_decisions()

        lines = self.review()
        self.assertEqual(
            lines,
            [
                ["pending", ["conflict", "A2", "S2"],
                 ["conflict", ["A2", "S2"], [4, 5]]],
                ["invalid", ["duplicate", "A2", "S2"], "changed",
                 ["ignore", "r2"], DUP_FINDING, DUP_FINDING_CHANGED],
                ["pending", ["duplicate", "A2", "S2"], DUP_FINDING_CHANGED],
                ["kept", ["invalid", 2, "qty"], ["confirm", "r1"],
                 INVALID_FINDING],
                ["summary", 1, 1, 2, old_hash, new_hash],
            ],
        )

    def test_resolved_findings_end_with_null(self) -> None:
        old_hash = self.store("old", OLD_CSV)
        new_hash = self.store("new", RESOLVED_CSV)
        self.seed_decisions()

        lines = self.review()
        self.assertEqual(
            lines,
            [
                ["invalid", ["duplicate", "A2", "S2"], "resolved",
                 ["ignore", "r2"], DUP_FINDING, None],
                ["invalid", ["invalid", 2, "qty"], "resolved",
                 ["confirm", "r1"], INVALID_FINDING, None],
                ["summary", 0, 2, 0, old_hash, new_hash],
            ],
        )

    def test_no_decisions_makes_everything_pending(self) -> None:
        self.store("old", OLD_CSV)
        self.store("new", OLD_CSV)
        lines = self.review()
        self.assertEqual([line[0] for line in lines[:-1]],
                         ["pending", "pending", "pending"])
        self.assertEqual(lines[-1], ["summary", 0, 0, 3, lines[-1][4],
                                     lines[-1][5]])

    def test_missing_batch_is_fatal_without_partial_results(self) -> None:
        self.store("old", OLD_CSV)
        out = io.BytesIO()
        with self.assertRaises(AuditError):
            run_review(self.db, "old", "ghost", stdout=out)
        self.assertEqual(out.getvalue(), b"")

        output = Path(self.dir.name) / "review.jsonl"
        output.write_text("OLD CONTENT")
        with self.assertRaises(AuditError):
            run_review(self.db, "ghost", "old", str(output))
        self.assertEqual(output.read_text(), "OLD CONTENT")

    def test_output_is_atomically_replaced(self) -> None:
        self.store("old", OLD_CSV)
        self.store("new", RESOLVED_CSV)
        self.seed_decisions()
        output = Path(self.dir.name) / "review.jsonl"
        output.write_text("OLD CONTENT")
        code = run_review(self.db, "old", "new", str(output))
        self.assertEqual(code, 0)
        lines = parse_lines(output.read_text())
        self.assertEqual(lines[-1][0], "summary")
        leftovers = [
            p.name for p in Path(self.dir.name).iterdir()
            if p.name.startswith(".review")
        ]
        self.assertEqual(leftovers, [])

    def test_cli_end_to_end(self) -> None:
        self.store("old", OLD_CSV)
        self.store("new", CHANGED_CSV)
        self.seed_decisions()

        result = run_cli("review-decisions", "--db", self.db, "old", "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertIn("invalid", [line[0] for line in lines])
        self.assertEqual(lines[-1][0], "summary")

        missing = run_cli("review-decisions", "--db", self.db, "old", "ghost")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("ghost", missing.stderr)
        self.assertEqual(missing.stdout, "")

        output = Path(self.dir.name) / "out.jsonl"
        to_file = run_cli("review-decisions", "--db", self.db, "old", "new",
                          "--output", str(output))
        self.assertEqual(to_file.returncode, 0, to_file.stderr)
        self.assertEqual(to_file.stdout, "")
        self.assertEqual(parse_lines(output.read_text())[-1][0], "summary")


if __name__ == "__main__":
    unittest.main()
