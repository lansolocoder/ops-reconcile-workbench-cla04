"""Checks for traceable corrections (``propose-fix`` and ``apply-fixes``)."""

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ops_workbench.decisions import run_decide
from ops_workbench.fixes import (
    FixConflictError,
    _ordered_edits,
    apply_patches,
    parse_patch,
    run_apply_fixes,
    run_propose_fix,
    validate_patch,
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
SCHEMA_OBJ = json.loads(SCHEMA)
HEADER = "oid,sku,qty,status,updated_at,note\n"
# Record 2 has an invalid qty; records 3 and 4 form a duplicate/conflict group.
CSV_TEXT = (
    HEADER
    + "A1,S1,0,open,2024-01-02T03:04:05Z,x\n"
    + "A2,S2,3,open,2024-01-02T03:04:05Z,y\n"
    + "A2,S2,4,open,2024-01-02T03:04:05Z,z\n"
)
QTY_ID = '["invalid",2,"qty"]'
DUP_ID = '["duplicate","A2","S2"]'
CONFLICT_ID = '["conflict","A2","S2"]'
TS = "2024-01-02T03:04:05Z"


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


class ParsePatchTests(unittest.TestCase):
    def test_accepts_a_valid_patch_and_preserves_it(self) -> None:
        patch = parse_patch('[ [2, "qty", "5"], [3, "status", "open"] ]')
        self.assertEqual(patch, [[2, "qty", "5"], [3, "status", "open"]])

    def test_rejects_bad_shapes(self) -> None:
        bad = [
            "[]",
            "{}",
            "not json",
            '"x"',
            "[1]",
            "[[2, \"qty\"]]",
            "[[2, \"qty\", \"5\", \"x\"]]",
            "[[0, \"qty\", \"5\"]]",
            "[[-2, \"qty\", \"5\"]]",
            "[[2.0, \"qty\", \"5\"]]",
            "[[true, \"qty\", \"5\"]]",
            "[[2, \"nope\", \"5\"]]",
            "[[2, \"qty\", 5]]",
            "[[2, \"qty\", null]]",
            "[[2, \"qty\", \"5\"], [2, \"qty\", \"6\"]]",
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(AuditError) as ctx:
                    parse_patch(raw)
                self.assertEqual(ctx.exception.filename, "PATCH")

    def test_duplicate_cell_target_is_rejected_even_across_order(self) -> None:
        with self.assertRaises(AuditError):
            parse_patch('[[3,"qty","1"],[2,"qty","1"],[3,"qty","2"]]')


class ValidatePatchTests(unittest.TestCase):
    def test_invalid_finding_is_locked_to_its_identity_cell(self) -> None:
        finding = ["invalid", 2, "qty", "0"]
        validate_patch([[2, "qty", "5"]], finding)
        for patch in [
            [[2, "status", "open"]],
            [[3, "qty", "5"]],
        ]:
            with self.subTest(patch=patch):
                with self.assertRaises(AuditError):
                    validate_patch(patch, finding)

    def test_group_finding_is_locked_to_listed_records(self) -> None:
        finding = ["duplicate", ["A2", "S2"], [3, 4]]
        validate_patch([[4, "qty", "3"], [3, "status", "cancelled"]], finding)
        with self.assertRaises(AuditError):
            validate_patch([[2, "qty", "3"]], finding)

    def test_new_values_must_pass_field_validation(self) -> None:
        finding = ["conflict", ["A2", "S2"], [3, 4]]
        for patch in [
            [[3, "qty", "0"]],
            [[3, "qty", "-5"]],
            [[3, "status", "maybe"]],
            [[3, "updated_at", "notadate"]],
            [[3, "sku", "  "]],
        ]:
            with self.subTest(patch=patch):
                with self.assertRaises(AuditError):
                    validate_patch(patch, finding)
        # order_id/sku trim before checking, the rest match verbatim
        validate_patch([[3, "sku", "  S9  "]], finding)
        with self.assertRaises(AuditError):
            validate_patch([[3, "updated_at", f"  {TS}  "]], finding)


class OrderedEditsTests(unittest.TestCase):
    def test_orders_by_identity_then_record_then_field(self) -> None:
        proposals = [
            (["duplicate", "A2", "S2"], ["duplicate", ["A2", "S2"], [3, 4]],
             [[4, "qty", "1"], [3, "sku", "S9"], [3, "qty", "1"]]),
            (["invalid", 2, "qty"], ["invalid", 2, "qty", "0"],
             [[2, "qty", "5"]]),
            (["conflict", "A2", "S2"], ["conflict", ["A2", "S2"], [3, 4]],
             [[3, "status", "open"]]),
        ]
        self.assertEqual(
            _ordered_edits(proposals),
            [
                (3, "status", "open"),   # conflict identity first
                (3, "qty", "1"),         # duplicate identity, record 3
                (3, "sku", "S9"),
                (4, "qty", "1"),         # same identity, record 4
                (2, "qty", "5"),         # invalid identity last
            ],
        )


class ApplyPatchesTests(unittest.TestCase):
    def test_changes_only_targeted_cells_and_normalizes_line_endings(self) -> None:
        data = ("﻿" + HEADER).replace("\n", "\r\n").encode("utf-8")
        data += (
            "A1,S1,0,open,2024-01-02T03:04:05Z,\"x, y\"\r\n"
            "\r\n"  # blank inter-record line still occupies record number 3
        ).encode("utf-8")
        out = apply_patches(data, SCHEMA_OBJ, [(2, "qty", "7")], "orders.csv")
        text = out.decode("utf-8")
        self.assertFalse(text.startswith("﻿"))
        self.assertEqual(
            text.splitlines(keepends=True),
            [
                "oid,sku,qty,status,updated_at,note\n",
                "A1,S1,7,open,2024-01-02T03:04:05Z,\"x, y\"\n",
                "\n",
            ],
        )

    def test_blank_record_cannot_be_patched(self) -> None:
        data = (HEADER + "\n").encode()
        with self.assertRaises(AuditError):
            apply_patches(data, SCHEMA_OBJ, [(2, "qty", "7")], "orders.csv")


class ProposeFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.input.write_bytes(CSV_TEXT.encode())
        self.db = str(base / "batches.db")
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id="b1")
        self.assertEqual(code, 1)

    def proposals(self) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            try:
                return conn.execute(
                    "SELECT batch_id, identity_json, finding_json, patch_json "
                    "FROM fix_proposals ORDER BY identity_json"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        finally:
            conn.close()

    def test_stores_proposal_after_fix_decision(self) -> None:
        self.assertEqual(run_decide(self.db, "b1", QTY_ID, "fix", "r"), 0)
        self.assertEqual(run_propose_fix(self.db, "b1", QTY_ID, '[[2,"qty","5"]]'), 0)
        rows = self.proposals()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "b1")
        self.assertEqual(json.loads(rows[0][1]), ["invalid", 2, "qty"])
        self.assertEqual(json.loads(rows[0][2]), ["invalid", 2, "qty", "0"])
        self.assertEqual(json.loads(rows[0][3]), [[2, "qty", "5"]])

    def test_group_proposal_may_target_every_listed_record(self) -> None:
        self.assertEqual(run_decide(self.db, "b1", CONFLICT_ID, "fix", "r"), 0)
        code = run_propose_fix(
            self.db, "b1", CONFLICT_ID, '[[4,"qty","3"],[3,"qty","3"]]'
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.proposals()), 1)

    def test_identical_proposal_is_idempotent(self) -> None:
        self.assertEqual(run_decide(self.db, "b1", QTY_ID, "fix", "r"), 0)
        for _ in range(2):
            self.assertEqual(
                run_propose_fix(self.db, "b1", QTY_ID, '[[2,"qty","5"]]'), 0
            )
        self.assertEqual(len(self.proposals()), 1)

    def test_same_identity_different_patch_exits_three_without_writing(self) -> None:
        self.assertEqual(run_decide(self.db, "b1", QTY_ID, "fix", "r"), 0)
        self.assertEqual(run_propose_fix(self.db, "b1", QTY_ID, '[[2,"qty","5"]]'), 0)
        before = self.proposals()
        with self.assertRaises(FixConflictError):
            run_propose_fix(self.db, "b1", QTY_ID, '[[2,"qty","9"]]')
        self.assertEqual(self.proposals(), before)

    def test_errors_exit_two_and_never_create_the_table(self) -> None:
        cases = [
            # no decision at all
            (QTY_ID, '[[2,"qty","5"]]', "decision"),
            # decision is confirm, not fix
        ]
        self.assertEqual(run_decide(self.db, "b1", DUP_ID, "confirm", "r"), 0)
        cases.append((DUP_ID, '[[3,"order_id","A9"]]', "fix"))
        for ident, patch, fragment in cases:
            with self.subTest(ident=ident):
                with self.assertRaises(AuditError) as ctx:
                    run_propose_fix(self.db, "b1", ident, patch)
                self.assertIn(fragment, ctx.exception.message)

        # A fix decision on an identity the batch does not know is impossible;
        # a malformed id or an unknown identity is rejected before writing.
        self.assertEqual(run_decide(self.db, "b1", QTY_ID, "fix", "r"), 0)
        for ident, patch in [
            ("not json", '[[2,"qty","5"]]'),
            ('["invalid",99,"qty"]', '[[99,"qty","5"]]'),
            ("ghost", '[[2,"qty","5"]]'),
        ]:
            with self.subTest(ident=ident):
                with self.assertRaises(AuditError):
                    run_propose_fix(self.db, "b1", ident, patch)

        # Invalid patch content despite the fix decision.
        for patch in [
            "[]",
            "[[2,\"qty\",\"0\"]]",            # fails field validation
            "[[3,\"qty\",\"5\"]]",            # outside invalid scope
        ]:
            with self.subTest(patch=patch):
                with self.assertRaises(AuditError):
                    run_propose_fix(self.db, "b1", QTY_ID, patch)

        # The failing confirm-case runs happened before any fix proposal
        # existed; the table must be empty.
        self.assertEqual(self.proposals(), [])

    def test_unknown_batch_and_database_exit_two(self) -> None:
        with self.assertRaises(AuditError):
            run_propose_fix(self.db, "ghost", QTY_ID, '[[2,"qty","5"]]')
        missing = str(Path(self.dir.name) / "absent.db")
        with self.assertRaises(AuditError):
            run_propose_fix(missing, "b1", QTY_ID, '[[2,"qty","5"]]')
        self.assertFalse(Path(missing).exists())

    def test_cli_exit_codes(self) -> None:
        ok = run_cli("decide", "--db", self.db, "b1", QTY_ID, "fix", "note")
        self.assertEqual(ok.returncode, 0, ok.stderr)

        proposed = run_cli(
            "propose-fix", "--db", self.db, "b1", QTY_ID, '[[2,"qty","5"]]'
        )
        self.assertEqual(proposed.returncode, 0, proposed.stderr)
        self.assertEqual(proposed.stdout, "")

        again = run_cli(
            "propose-fix", "--db", self.db, "b1", QTY_ID, '[[2,"qty","5"]]'
        )
        self.assertEqual(again.returncode, 0, again.stderr)

        conflict = run_cli(
            "propose-fix", "--db", self.db, "b1", QTY_ID, '[[2,"qty","9"]]'
        )
        self.assertEqual(conflict.returncode, 3)
        self.assertIn("different patch", conflict.stderr)
        self.assertEqual(conflict.stdout, "")

        bad = run_cli(
            "propose-fix", "--db", self.db, "b1", QTY_ID, '[[2,"qty","0"]]'
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("validation", bad.stderr)

        nodecision = run_cli(
            "propose-fix", "--db", self.db, "b1", DUP_ID, '[[3,"order_id","A9"]]'
        )
        self.assertEqual(nodecision.returncode, 2)


class ApplyFixesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.input.write_bytes(CSV_TEXT.encode())
        self.db = str(base / "batches.db")
        self.output = base / "fixed.csv"
        self.report = base / "fixed.jsonl"
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id="src")
        self.assertEqual(code, 1)
        self.source_hash = hashlib.sha256(CSV_TEXT.encode()).hexdigest()

    def fix(self, ident: str, patch: str, reason: str = "r") -> None:
        self.assertEqual(run_decide(self.db, "src", ident, "fix", reason), 0)
        self.assertEqual(run_propose_fix(self.db, "src", ident, patch), 0)

    def derived(self) -> tuple | None:
        conn = sqlite3.connect(self.db)
        try:
            try:
                return conn.execute(
                    "SELECT source_batch_id, input_sha256, schema_json, "
                    "findings_json, source_sha256, snapshot_json "
                    "FROM derived_batches WHERE derived_id = 'der'"
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        finally:
            conn.close()

    def test_applies_proposals_reaudits_and_traces_derived_batch(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        # Resolve the conflict by giving record 4 the same qty as record 3:
        # the conflict disappears but the duplicate remains.
        self.fix(CONFLICT_ID, '[[4,"qty","3"]]')

        out = io.BytesIO()
        code = run_apply_fixes(
            self.db, "src", "der", str(self.input), str(self.output),
            stdout=out,
        )
        self.assertEqual(code, 0)

        # The fixed CSV only differs in the two patched cells; the extra note
        # column and untouched cells survive.
        self.assertEqual(
            self.output.read_text(encoding="utf-8").splitlines(),
            [
                "oid,sku,qty,status,updated_at,note",
                "A1,S1,3,open,2024-01-02T03:04:05Z,x",
                "A2,S2,3,open,2024-01-02T03:04:05Z,y",
                "A2,S2,3,open,2024-01-02T03:04:05Z,z",
            ],
        )
        raw = self.output.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", raw)

        # The re-audit runs under the source schema: only the duplicate stays.
        lines = parse_lines(out.getvalue())
        self.assertEqual(
            lines,
            [
                ["duplicate", ["A2", "S2"], [3, 4]],
                ["summary", 3, 1, hashlib.sha256(raw).hexdigest()],
            ],
        )

        # INPUT is untouched.
        self.assertEqual(self.input.read_bytes(), CSV_TEXT.encode())

        row = self.derived()
        self.assertIsNotNone(row)
        source_id, new_hash, schema_json, findings_json, source_hash, snapshot = row
        self.assertEqual(source_id, "src")
        self.assertEqual(new_hash, hashlib.sha256(raw).hexdigest())
        self.assertEqual(json.loads(schema_json), SCHEMA_OBJ)
        self.assertEqual(
            json.loads(findings_json), [["duplicate", ["A2", "S2"], [3, 4]]]
        )
        self.assertEqual(source_hash, self.source_hash)
        decisions_snap, proposals_snap = json.loads(snapshot)
        self.assertEqual(
            sorted(d[0] for d in decisions_snap),
            [["conflict", "A2", "S2"], ["invalid", 2, "qty"]],
        )
        self.assertTrue(all(d[1] == "fix" for d in decisions_snap))
        self.assertEqual(
            sorted(p[0] for p in proposals_snap),
            [["conflict", "A2", "S2"], ["invalid", 2, "qty"]],
        )
        self.assertIn(
            [["conflict", "A2", "S2"],
             ["conflict", ["A2", "S2"], [3, 4]],
             [[4, "qty", "3"]]],
            proposals_snap,
        )

    def test_report_to_file_and_idempotent_rerun(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        for _ in range(2):
            code = run_apply_fixes(
                self.db, "src", "der", str(self.input), str(self.output),
                str(self.report),
            )
            self.assertEqual(code, 0)
        lines = parse_lines(self.report.read_text())
        self.assertEqual(lines[-1][0], "summary")
        # Only one derived row after the idempotent rerun.
        conn = sqlite3.connect(self.db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM derived_batches"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_fully_fixing_everything_audits_clean(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        self.fix(DUP_ID, '[[4,"order_id","A3"],[4,"sku","S3"]]')
        out = io.BytesIO()
        self.assertEqual(
            run_apply_fixes(self.db, "src", "der", str(self.input),
                            str(self.output), stdout=out),
            0,
        )
        lines = parse_lines(out.getvalue())
        self.assertEqual(lines[-1][:3], ["summary", 3, 0])

    def test_input_hash_mismatch_is_fatal_and_changes_nothing(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        other = Path(self.dir.name) / "other.csv"
        other.write_bytes(b"oid,sku\n")
        self.output.write_text("OLD CSV")
        self.report.write_text("OLD REPORT")
        with self.assertRaises(AuditError):
            run_apply_fixes(self.db, "src", "der", str(other), str(self.output),
                            str(self.report))
        self.assertEqual(self.output.read_text(), "OLD CSV")
        self.assertEqual(self.report.read_text(), "OLD REPORT")
        self.assertIsNone(self.derived())
        leftovers = [p.name for p in Path(self.dir.name).iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_missing_proposal_for_fix_decision_is_fatal(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        # A second fix decision without a proposal.
        self.assertEqual(run_decide(self.db, "src", DUP_ID, "fix", "r"), 0)
        with self.assertRaises(AuditError) as ctx:
            run_apply_fixes(self.db, "src", "der", str(self.input),
                            str(self.output))
        self.assertIn("no proposal", ctx.exception.message)
        self.assertFalse(self.output.exists())
        self.assertIsNone(self.derived())

    def test_cross_proposal_duplicate_target_is_fatal(self) -> None:
        # Two findings both list record 3 (an invalid cell and the group);
        # proposals patching the same cell from both are rejected at apply.
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        self.fix(CONFLICT_ID, '[[3,"qty","3"]]')
        self.fix(DUP_ID, '[[3,"qty","3"],[4,"order_id","A3"],[4,"sku","S3"]]')
        with self.assertRaises(AuditError) as ctx:
            run_apply_fixes(self.db, "src", "der", str(self.input),
                            str(self.output))
        self.assertIn("more than once", ctx.exception.message)
        self.assertFalse(self.output.exists())
        self.assertIsNone(self.derived())

    def test_stale_decision_or_proposal_is_fatal_and_changes_nothing(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        self.output.write_text("OLD CSV")
        # Simulate a stale decision: its stored finding no longer matches the
        # immutable batch finding (e.g. database touched out of band).
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE decisions SET finding_json = ? WHERE batch_id = 'src'",
                (json.dumps(["invalid", 2, "qty", "9"]),),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(AuditError) as ctx:
            run_apply_fixes(self.db, "src", "der", str(self.input),
                            str(self.output))
        self.assertIn("no longer", ctx.exception.message)
        self.assertEqual(self.output.read_text(), "OLD CSV")
        self.assertIsNone(self.derived())

    def test_unknown_source_batch_and_database_exit_two(self) -> None:
        with self.assertRaises(AuditError):
            run_apply_fixes(self.db, "ghost", "der", str(self.input),
                            str(self.output))
        missing = str(Path(self.dir.name) / "absent.db")
        with self.assertRaises(AuditError):
            run_apply_fixes(missing, "src", "der", str(self.input),
                            str(self.output))

    def test_output_must_differ_from_input(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        with self.assertRaises(AuditError):
            run_apply_fixes(self.db, "src", "der", str(self.input),
                            str(self.input))
        self.assertEqual(self.input.read_bytes(), CSV_TEXT.encode())

    def test_conflicting_derived_id_exits_three_and_keeps_old_outputs(self) -> None:
        self.fix(QTY_ID, '[[2,"qty","3"]]')
        self.assertEqual(
            run_apply_fixes(self.db, "src", "der", str(self.input),
                            str(self.output), str(self.report)),
            0,
        )
        before_csv = self.output.read_bytes()
        before_report = self.report.read_bytes()

        # Same derived id, different content (an extra proposal).
        self.fix(DUP_ID, '[[4,"order_id","A3"],[4,"sku","S3"]]')
        with self.assertRaises(FixConflictError):
            run_apply_fixes(self.db, "src", "der", str(self.input),
                            str(self.output), str(self.report))
        self.assertEqual(self.output.read_bytes(), before_csv)
        self.assertEqual(self.report.read_bytes(), before_report)

    def test_cli_end_to_end(self) -> None:
        self.assertEqual(
            run_cli("decide", "--db", self.db, "src", QTY_ID, "fix", "note").returncode,
            0,
        )
        proposed = run_cli(
            "propose-fix", "--db", self.db, "src", QTY_ID, '[[2,"qty","3"]]'
        )
        self.assertEqual(proposed.returncode, 0, proposed.stderr)

        result = run_cli(
            "apply-fixes", "--db", self.db, "src", "der", str(self.input),
            "--output", str(self.output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[-1][0], "summary")
        self.assertTrue(self.output.exists())

        to_file = run_cli(
            "apply-fixes", "--db", self.db, "src", "der", str(self.input),
            "--output", str(self.output), "--report", str(self.report),
        )
        self.assertEqual(to_file.returncode, 0, to_file.stderr)
        self.assertEqual(to_file.stdout, "")
        self.assertTrue(self.report.exists())

        # INPUT hash mismatch through the CLI.
        other = Path(self.dir.name) / "other.csv"
        other.write_text(HEADER + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n")
        mismatch = run_cli(
            "apply-fixes", "--db", self.db, "src", "der2", str(other),
            "--output", str(Path(self.dir.name) / "x.csv"),
        )
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn("hash", mismatch.stderr)

        # A reused derived id with a different source is a conflict.
        other_db_run = run_cli(
            "audit-orders", "--schema", SCHEMA, str(other),
            "--db", self.db, "--batch", "src2",
        )
        self.assertEqual(other_db_run.returncode, 0, other_db_run.stderr)
        conflict = run_cli(
            "apply-fixes", "--db", self.db, "src2", "der", str(other),
            "--output", str(Path(self.dir.name) / "y.csv"),
        )
        self.assertEqual(conflict.returncode, 3)
        self.assertIn("der", conflict.stderr)


if __name__ == "__main__":
    unittest.main()
