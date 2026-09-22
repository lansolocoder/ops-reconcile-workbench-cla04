"""Checks for traceable corrections (``propose-fix`` and ``apply-fixes``)."""

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
from unittest.mock import patch

from ops_workbench.fixes import (
    FixConflictError,
    run_apply_fixes,
    run_propose_fix,
)
from ops_workbench import fixes as fixes_mod
from ops_workbench.orders_audit import AuditError, run_audit
from ops_workbench.decisions import run_decide

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
TS = "2024-01-02T03:04:05Z"
HEADER = "oid,sku,qty,status,updated_at,note\n"
# Record 2: invalid qty (excluded from groups).  Records 3 and 4 form a
# duplicate group (A1/S1, identical).  Record 5: invalid empty sku.
CSV_WITH_FINDINGS = (
    HEADER
    + f"A0,S0,0,open,{TS},n1\n"
    + f"A1,S1,2,open,{TS},n2\n"
    + f"A1,S1,2,open,{TS},n3\n"
    + f"A2,,3,open,{TS},n5\n"
)
QTY_ID = '["invalid",2,"qty"]'
SKU_ID = '["invalid",5,"sku"]'
DUP_ID = '["duplicate","A1","S1"]'
# qty 2 -> 5 (record 2 keeps its own A0/S0 group); empty sku at 5 -> S9;
# record 4 leaves the A1/S1 group (sku S7).
QTY_PATCH = '[[2,"qty","5"]]'
SKU_PATCH = '[[5,"sku","S9"]]'
DUP_PATCH = '[[4,"sku","S7"]]'
EXPECTED_CORRECTED = (
    HEADER
    + f"A0,S0,5,open,{TS},n1\n"
    + f"A1,S1,2,open,{TS},n2\n"
    + f"A1,S7,2,open,{TS},n3\n"
    + f"A2,S9,3,open,{TS},n5\n"
)


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


class ApplyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.input = base / "orders.csv"
        self.input.write_bytes(CSV_WITH_FINDINGS.encode())
        self.db = str(base / "batches.db")
        code = run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                         db_path=self.db, batch_id="src")
        self.assertEqual(code, 1)
        self.source_hash = hashlib.sha256(
            CSV_WITH_FINDINGS.encode()
        ).hexdigest()

    def decide_all(self) -> None:
        self.assertEqual(run_decide(self.db, "src", QTY_ID, "fix", "q"), 0)
        self.assertEqual(run_decide(self.db, "src", SKU_ID, "fix", "s"), 0)
        self.assertEqual(run_decide(self.db, "src", DUP_ID, "fix", "d"), 0)

    def propose_all(self) -> None:
        self.assertEqual(run_propose_fix(self.db, "src", QTY_ID, QTY_PATCH), 0)
        self.assertEqual(run_propose_fix(self.db, "src", SKU_ID, SKU_PATCH), 0)
        self.assertEqual(run_propose_fix(self.db, "src", DUP_ID, DUP_PATCH), 0)


class ProposeFixTests(ApplyFixture):
    def proposals(self) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            try:
                return conn.execute(
                    "SELECT batch_id, identity_json, finding_json, patch_json "
                    "FROM fix_proposals ORDER BY identity_json"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        finally:
            conn.close()

    def test_stores_original_finding_and_canonical_patch(self) -> None:
        run_decide(self.db, "src", QTY_ID, "fix", "q")
        code = run_propose_fix(self.db, "src", QTY_ID, '[ [ 2 , "qty" , "5" ] ]')
        self.assertEqual(code, 0)
        rows = self.proposals()
        self.assertEqual(len(rows), 1)
        batch_id, identity_json, finding_json, patch_json = rows[0]
        self.assertEqual(batch_id, "src")
        self.assertEqual(identity_json, '["invalid",2,"qty"]')
        self.assertEqual(finding_json, '["invalid",2,"qty","0"]')
        self.assertEqual(patch_json, '[[2,"qty","5"]]')

    def test_repeat_is_idempotent_different_patch_conflicts(self) -> None:
        run_decide(self.db, "src", QTY_ID, "fix", "q")
        for _ in range(2):
            self.assertEqual(
                run_propose_fix(self.db, "src", QTY_ID, QTY_PATCH), 0
            )
        self.assertEqual(len(self.proposals()), 1)
        with self.assertRaises(FixConflictError):
            run_propose_fix(self.db, "src", QTY_ID, '[[2,"qty","6"]]')
        # The stored proposal is untouched by the rejected call.
        self.assertEqual(self.proposals()[0][3], '[[2,"qty","5"]]')

    def test_requires_a_matching_fix_decision(self) -> None:
        # No decision at all.
        with self.assertRaises(AuditError) as ctx:
            run_propose_fix(self.db, "src", QTY_ID, QTY_PATCH)
        self.assertIn("no fix decision", ctx.exception.message)

        # A different action does not qualify.
        run_decide(self.db, "src", QTY_ID, "confirm", "c")
        with self.assertRaises(AuditError) as ctx:
            run_propose_fix(self.db, "src", QTY_ID, QTY_PATCH)
        self.assertIn("not a fix decision", ctx.exception.message)
        self.assertEqual(self.proposals(), [])

    def test_invalid_finding_may_only_change_its_identity_cell(self) -> None:
        run_decide(self.db, "src", QTY_ID, "fix", "q")
        for patch in [
            '[[3,"qty","5"]]',   # wrong record
            '[[2,"sku","S1"]]',  # wrong field
        ]:
            with self.subTest(patch=patch):
                with self.assertRaises(AuditError):
                    run_propose_fix(self.db, "src", QTY_ID, patch)
        self.assertEqual(self.proposals(), [])

    def test_group_finding_may_only_change_listed_records(self) -> None:
        run_decide(self.db, "src", DUP_ID, "fix", "d")
        # Any of the five fields on a listed record is allowed.
        self.assertEqual(
            run_propose_fix(self.db, "src", DUP_ID, '[[3,"qty","8"]]'), 0
        )
        # Record 2 is invalid and therefore not listed by the group.
        with self.assertRaises(AuditError):
            run_propose_fix(self.db, "src", DUP_ID, '[[2,"sku","S7"]]')

    def test_targets_must_be_unique_and_values_valid(self) -> None:
        run_decide(self.db, "src", DUP_ID, "fix", "d")
        bad = [
            ('[[3,"sku","S7"],[3,"sku","S8"]]', "more than once"),
            ('[[3,"qty","0"]]', "fails field validation"),
            ('[[3,"status","closed"]]', "fails field validation"),
            (f'[[3,"updated_at","nope"]]', "fails field validation"),
            ('[[3,"order_id",""]]', "fails field validation"),
        ]
        for patch, fragment in bad:
            with self.subTest(patch=patch):
                with self.assertRaises(AuditError) as ctx:
                    run_propose_fix(self.db, "src", DUP_ID, patch)
                self.assertIn(fragment, ctx.exception.message)

    def test_bad_parameters_exit_two_without_writing(self) -> None:
        run_decide(self.db, "src", QTY_ID, "fix", "q")
        cases = [
            ("not json", "PATCH"),
            ("{}", "non-empty JSON array"),
            ("[]", "non-empty JSON array"),
            ('[["x","qty","5"]]', "record number"),
            ('[[2,"nope","5"]]', "field"),
            ('[[2,"qty",5]]', "string"),
            ('[[2,"qty"]]', "must be"),
        ]
        for patch, fragment in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(AuditError) as ctx:
                    run_propose_fix(self.db, "src", QTY_ID, patch)
                self.assertIn(fragment, ctx.exception.message)
        with self.assertRaises(AuditError):
            run_propose_fix(self.db, "src", "not json", QTY_PATCH)
        with self.assertRaises(AuditError):
            run_propose_fix(self.db, "ghost", QTY_ID, QTY_PATCH)
        self.assertEqual(self.proposals(), [])

    def test_missing_database_is_fatal(self) -> None:
        missing = str(Path(self.dir.name) / "absent.db")
        with self.assertRaises(AuditError):
            run_propose_fix(missing, "src", QTY_ID, QTY_PATCH)
        self.assertFalse(Path(missing).exists())

    def test_cli_exit_codes(self) -> None:
        run_decide(self.db, "src", QTY_ID, "fix", "q")
        ok = run_cli("propose-fix", "--db", self.db, "src", QTY_ID, QTY_PATCH)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout, "")

        again = run_cli("propose-fix", "--db", self.db, "src", QTY_ID, QTY_PATCH)
        self.assertEqual(again.returncode, 0)

        conflict = run_cli(
            "propose-fix", "--db", self.db, "src", QTY_ID, '[[2,"qty","6"]]'
        )
        self.assertEqual(conflict.returncode, 3)
        self.assertIn("src", conflict.stderr)

        bad = run_cli("propose-fix", "--db", self.db, "src", QTY_ID, "[]")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("PATCH", bad.stderr)


class ApplyFixesTests(ApplyFixture):
    def apply(self, derived="der", **kwargs):
        output = kwargs.pop("output", None) or str(Path(self.dir.name) / "fixed.csv")
        return run_apply_fixes(
            self.db,
            "src",
            derived,
            str(self.input),
            output,
            stdout=kwargs.pop("stdout", None),
            report_path=kwargs.pop("report", None),
        ), output

    def derived_rows(self) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            try:
                return conn.execute(
                    "SELECT derived_id, input_sha256, schema_json, findings_json, "
                    "source_batch_id, snapshot_json FROM derived_batches"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        finally:
            conn.close()

    def test_applies_all_fixes_reaudits_clean_and_preserves_input(self) -> None:
        self.decide_all()
        self.propose_all()
        before = self.input.read_bytes()
        out = io.BytesIO()
        code, output_path = self.apply(stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(Path(output_path).read_text(), EXPECTED_CORRECTED)
        # The input file is never modified.
        self.assertEqual(self.input.read_bytes(), before)
        # The re-audit of the corrected CSV is clean; report to stdout.
        lines = parse_lines(out.getvalue())
        self.assertEqual(lines[-1][0], "summary")
        self.assertEqual(lines[-1][1], 4)       # data rows
        self.assertEqual(lines[-1][2], 0)       # findings
        self.assertEqual(lines[:-1], [])

    def test_derived_batch_records_traceability(self) -> None:
        self.decide_all()
        self.propose_all()
        self.apply()
        rows = self.derived_rows()
        self.assertEqual(len(rows), 1)
        derived_id, digest, schema_json, findings_json, source_id, snapshot_json = rows[0]
        self.assertEqual(derived_id, "der")
        self.assertEqual(source_id, "src")
        self.assertEqual(
            digest, hashlib.sha256(EXPECTED_CORRECTED.encode()).hexdigest()
        )
        self.assertEqual(json.loads(schema_json), json.loads(SCHEMA))
        self.assertEqual(json.loads(findings_json), [])
        snapshot = json.loads(snapshot_json)
        self.assertEqual(
            [e["identity"] for e in snapshot],
            [
                ["duplicate", "A1", "S1"],
                ["invalid", 2, "qty"],
                ["invalid", 5, "sku"],
            ],
        )
        dup_entry = snapshot[0]
        self.assertEqual(dup_entry["reason"], "d")
        self.assertEqual(dup_entry["proposal"]["patch"], [[4, "sku", "S7"]])
        self.assertEqual(dup_entry["finding"], ["duplicate", ["A1", "S1"], [3, 4]])

    def test_idempotent_same_derived_id(self) -> None:
        self.decide_all()
        self.propose_all()
        code1, out1 = self.apply()
        code2, out2 = self.apply()
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(len(self.derived_rows()), 1)
        self.assertEqual(Path(out1).read_bytes(), Path(out2).read_bytes())

    def test_same_derived_id_different_content_exits_three(self) -> None:
        self.decide_all()
        self.propose_all()
        self.apply(derived="shared")

        # A second, distinct source batch reusing the same derived id must
        # conflict rather than overwrite.
        other_input = Path(self.dir.name) / "other.csv"
        other_csv = HEADER + f"B9,S9,1,open,{TS},m\n"
        other_input.write_text(other_csv)
        run_audit(SCHEMA, str(other_input), stdout=io.BytesIO(),
                  db_path=self.db, batch_id="other")
        output = str(Path(self.dir.name) / "other_fixed.csv")
        with self.assertRaises(FixConflictError):
            run_apply_fixes(
                self.db, "other", "shared", str(other_input), output
            )
        self.assertFalse(Path(output).exists())

    def test_input_hash_mismatch_is_fatal_and_keeps_old_outputs(self) -> None:
        self.decide_all()
        self.propose_all()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        output.write_text("OLD CSV")
        report.write_text("OLD REPORT")
        self.input.write_text(HEADER + f"A9,S9,1,open,{TS},x\n")
        with self.assertRaises(AuditError) as ctx:
            run_apply_fixes(
                self.db, "src", "der", str(self.input), str(output),
                report_path=str(report),
            )
        self.assertIn("hash", ctx.exception.message)
        self.assertEqual(output.read_text(), "OLD CSV")
        self.assertEqual(report.read_text(), "OLD REPORT")
        self.assertEqual(self.derived_rows(), [])

    def test_every_fix_decision_must_have_a_proposal(self) -> None:
        self.decide_all()
        run_propose_fix(self.db, "src", QTY_ID, QTY_PATCH)
        # The sku and duplicate decisions lack proposals.
        with self.assertRaises(AuditError) as ctx:
            self.apply()
        self.assertIn("has no proposal", ctx.exception.message)
        self.assertEqual(self.derived_rows(), [])

    def test_stale_proposal_whose_finding_no_longer_matches_is_fatal(self) -> None:
        self.decide_all()
        self.propose_all()
        # Corrupt one stored proposal so its saved finding no longer matches
        # any finding of the (immutable) source batch.
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE fix_proposals SET finding_json = ? "
                "WHERE identity_json = ?",
                ('["duplicate",["A1","S1"],[3,4,9]]',
                 '["duplicate","A1","S1"]'),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(AuditError):
            self.apply()
        self.assertEqual(self.derived_rows(), [])

    def test_preserves_extra_columns_quoting_and_untouched_cells(self) -> None:
        csv_text = (
            HEADER
            + f'A1,S1,0,open,{TS},"keep, quote"\n'
            + f'A2,S2,3,open,{TS},"line1\nline2"\n'
        )
        self.input.write_bytes(csv_text.encode())
        run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                  db_path=self.db, batch_id="q")
        run_decide(self.db, "q", '["invalid",2,"qty"]', "fix", "r")
        run_propose_fix(self.db, "q", '["invalid",2,"qty"]', '[[2,"qty","5"]]')
        out = str(Path(self.dir.name) / "q.csv")
        code = run_apply_fixes(
            self.db, "q", "dq", str(self.input), out
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            Path(out).read_text(),
            HEADER
            + f'A1,S1,5,open,{TS},"keep, quote"\n'
            + f'A2,S2,3,open,{TS},"line1\nline2"\n',
        )

    def test_output_is_utf8_no_bom_with_lf_and_bom_input_is_accepted(self) -> None:
        csv_bytes = ("\ufeff" + CSV_WITH_FINDINGS).encode("utf-8").replace(
            b"\n", b"\r\n"
        )
        self.input.write_bytes(csv_bytes)
        # The BOM/CRLF bytes are the hash stored by the source batch.
        run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                  db_path=self.db, batch_id="bom")
        run_decide(self.db, "bom", QTY_ID, "fix", "q")
        run_propose_fix(self.db, "bom", QTY_ID, QTY_PATCH)
        run_decide(self.db, "bom", SKU_ID, "fix", "s")
        run_propose_fix(self.db, "bom", SKU_ID, SKU_PATCH)
        run_decide(self.db, "bom", DUP_ID, "fix", "d")
        run_propose_fix(self.db, "bom", DUP_ID, DUP_PATCH)
        out = str(Path(self.dir.name) / "bom_out.csv")
        code = run_apply_fixes(self.db, "bom", "dbom", str(self.input), out)
        self.assertEqual(code, 0)
        data = Path(out).read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))

    def test_report_file_and_csv_are_atomically_replaced(self) -> None:
        self.decide_all()
        self.propose_all()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        output.write_text("OLD CSV")
        report.write_text("OLD REPORT")
        code = run_apply_fixes(
            self.db, "src", "der", str(self.input), str(output),
            report_path=str(report),
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.read_text(), EXPECTED_CORRECTED)
        self.assertEqual(
            parse_lines(report.read_text())[-1][0], "summary"
        )
        leftovers = [
            p.name for p in Path(self.dir.name).iterdir()
            if p.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_output_failure_leaves_db_and_old_outputs_untouched(self) -> None:
        self.decide_all()
        self.propose_all()
        output = Path(self.dir.name) / "a_directory"
        output.mkdir()
        report = Path(self.dir.name) / "report.jsonl"
        report.write_text("OLD REPORT")
        with self.assertRaises(AuditError):
            run_apply_fixes(
                self.db, "src", "der", str(self.input), str(output),
                report_path=str(report),
            )
        self.assertEqual(report.read_text(), "OLD REPORT")
        self.assertEqual(self.derived_rows(), [])

    def test_missing_database_is_fatal(self) -> None:
        missing = str(Path(self.dir.name) / "absent.db")
        out = str(Path(self.dir.name) / "x.csv")
        with self.assertRaises(AuditError):
            run_apply_fixes(missing, "src", "der", str(self.input), out)
        self.assertFalse(Path(out).exists())

    def test_cli_end_to_end(self) -> None:
        for args in [
            ("decide", "--db", self.db, "src", QTY_ID, "fix", "q"),
            ("decide", "--db", self.db, "src", SKU_ID, "fix", "s"),
            ("decide", "--db", self.db, "src", DUP_ID, "fix", "d"),
            ("propose-fix", "--db", self.db, "src", QTY_ID, QTY_PATCH),
            ("propose-fix", "--db", self.db, "src", SKU_ID, SKU_PATCH),
            ("propose-fix", "--db", self.db, "src", DUP_ID, DUP_PATCH),
        ]:
            result = run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)

        output = str(Path(self.dir.name) / "fixed.csv")
        result = run_cli(
            "apply-fixes", "--db", self.db, "src", "der", str(self.input),
            "--output", output,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = parse_lines(result.stdout)
        self.assertEqual(lines[-1][:3], ["summary", 4, 0])
        self.assertEqual(Path(output).read_text(), EXPECTED_CORRECTED)

        report = str(Path(self.dir.name) / "report.jsonl")
        to_file = run_cli(
            "apply-fixes", "--db", self.db, "src", "der", str(self.input),
            "--output", output, "--report", report,
        )
        self.assertEqual(to_file.returncode, 0, to_file.stderr)
        self.assertEqual(to_file.stdout, "")
        self.assertTrue(Path(report).exists())

        # Hash mismatch via a tampered input.
        tampered = Path(self.dir.name) / "tampered.csv"
        tampered.write_text(HEADER + f"A9,S9,1,open,{TS},x\n")
        bad = run_cli(
            "apply-fixes", "--db", self.db, "src", "der2", str(tampered),
            "--output", str(Path(self.dir.name) / "t.csv"),
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("hash", bad.stderr)


class MergeAndRecoveryTests(unittest.TestCase):
    """Overlap merge ordering, snapshot content and output rollback."""

    CONFLICT_ID = '["conflict","A1","S1"]'
    DUP_ID = '["duplicate","A1","S1"]'

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        # Same oid/sku with a differing qty yields BOTH a duplicate and a
        # conflict finding over records 2 and 3, so two distinct findings
        # can legitimately target the same cell.
        self.csv_text = (
            "oid,sku,qty,status,updated_at\n"
            + f"A1,S1,2,open,{TS}\n"
            + f"A1,S1,7,open,{TS}\n"
        )
        self.input = base / "orders.csv"
        self.input.write_text(self.csv_text)
        self.db = str(base / "b.db")
        self.assertEqual(
            run_audit(SCHEMA, str(self.input), stdout=io.BytesIO(),
                      db_path=self.db, batch_id="s"),
            1,
        )

    def _propose_overlapping(self) -> None:
        # "conflict" sorts before "duplicate"; both write record 3's qty,
        # so only last-writer-wins by identity order can succeed.
        self.assertEqual(
            run_decide(self.db, "s", self.CONFLICT_ID, "fix", "  c  "), 0
        )
        self.assertEqual(
            run_decide(self.db, "s", self.DUP_ID, "fix", "d"), 0
        )
        self.assertEqual(
            run_propose_fix(self.db, "s", self.CONFLICT_ID,
                            '[[3,"qty","8"]]'), 0
        )
        self.assertEqual(
            run_propose_fix(self.db, "s", self.DUP_ID,
                            '[[3,"qty","9"],[2,"sku","S2"]]'), 0
        )

    def test_overlapping_findings_last_value_wins_by_identity_order(self) -> None:
        self._propose_overlapping()
        output = str(Path(self.dir.name) / "fixed.csv")
        code = run_apply_fixes(
            self.db, "s", "der", str(self.input), output
        )
        self.assertEqual(code, 0)
        rows = Path(output).read_text().splitlines()
        # conflict (8) is overwritten by duplicate (9) on the shared cell.
        self.assertEqual(rows[1], f"A1,S2,2,open,{TS}")
        self.assertEqual(rows[2], f"A1,S1,9,open,{TS}")

    def test_final_value_governs_reaudit_hash_and_snapshot(self) -> None:
        self._propose_overlapping()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        self.assertEqual(
            run_apply_fixes(
                self.db, "s", "der", str(self.input), str(output),
                report_path=str(report),
            ),
            0,
        )
        lines = parse_lines(report.read_text())
        # The merged correction fully resolves the group.
        self.assertEqual(lines[-1], [
            "summary", 2, 0,
            hashlib.sha256(output.read_bytes()).hexdigest(),
        ])
        conn = sqlite3.connect(self.db)
        try:
            digest, snapshot_json = conn.execute(
                "SELECT input_sha256, snapshot_json FROM derived_batches "
                "WHERE derived_id = 'der'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            digest, hashlib.sha256(output.read_bytes()).hexdigest()
        )
        snapshot = json.loads(snapshot_json)
        self.assertEqual(
            [e["identity"] for e in snapshot],
            [["conflict", "A1", "S1"], ["duplicate", "A1", "S1"]],
        )
        for entry in snapshot:
            self.assertEqual(entry["action"], "fix")
        self.assertEqual(snapshot[0]["reason"], "c")  # trimmed
        self.assertEqual(
            snapshot[0]["finding"], ["conflict", ["A1", "S1"], [2, 3]]
        )
        self.assertEqual(
            snapshot[0]["proposal"],
            {"finding": ["conflict", ["A1", "S1"], [2, 3]],
             "patch": [[3, "qty", "8"]]},
        )
        self.assertEqual(
            snapshot[1]["proposal"]["finding"],
            ["duplicate", ["A1", "S1"], [2, 3]],
        )
        self.assertEqual(
            snapshot[1]["proposal"]["patch"], [[3, "qty", "9"], [2, "sku", "S2"]]
        )

    def test_within_finding_triples_run_by_field_text_not_field_order(self) -> None:
        run_decide(self.db, "s", self.DUP_ID, "fix", "d")
        # Stored order: sku before qty; logical FIELDS order is sku before
        # qty too, but field *text* ascending is qty before sku.
        run_propose_fix(
            self.db, "s", self.DUP_ID, '[[2,"sku","S5"],[2,"qty","3"]]'
        )
        run_decide(self.db, "s", self.CONFLICT_ID, "fix", "c")
        run_propose_fix(self.db, "s", self.CONFLICT_ID, '[[3,"qty","8"]]')
        seen: list[tuple] = []
        real_apply = fixes_mod._apply_patches

        def spy(text, width, columns, changes, name):
            seen.extend(changes)
            return real_apply(text, width, columns, changes, name)

        output = str(Path(self.dir.name) / "spied.csv")
        with patch.object(fixes_mod, "_apply_patches", spy):
            self.assertEqual(
                run_apply_fixes(
                    self.db, "s", "der", str(self.input), output
                ),
                0,
            )
        # conflict identity first (record 3 qty), then duplicate's record 2
        # triples in field-text order: qty < sku.
        self.assertEqual(
            seen,
            [(3, "qty"), (2, "qty"), (2, "sku")],
        )

    def _patch_replace_failing_csv_once(self):
        """Return a patcher making the staged CSV replace fail once."""
        real_replace = fixes_mod.os.replace

        def flaky(src, dst, *args, **kwargs):
            name = os.path.basename(str(src))
            if (
                str(dst).endswith("fixed.csv")
                and name.endswith(".tmp")
                and ".restore." not in name
            ):
                raise OSError("simulated csv replace failure")
            return real_replace(src, dst, *args, **kwargs)

        return patch.object(fixes_mod.os, "replace", flaky)

    def test_replace_failure_restores_already_replaced_report(self) -> None:
        self._propose_overlapping()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        output.write_text("OLD CSV")
        report.write_text("OLD REPORT")
        with self._patch_replace_failing_csv_once(), self.assertRaises(AuditError):
            run_apply_fixes(
                self.db, "s", "der", str(self.input), str(output),
                report_path=str(report),
            )
        self.assertEqual(output.read_text(), "OLD CSV")
        self.assertEqual(report.read_text(), "OLD REPORT")
        self.assertEqual(self._derived_count("der"), 0)
        self.assertFalse(
            [p.name for p in Path(self.dir.name).iterdir()
             if p.name.endswith(".tmp")]
        )

    def test_replace_failure_removes_outputs_that_did_not_exist(self) -> None:
        self._propose_overlapping()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        with self._patch_replace_failing_csv_once(), self.assertRaises(AuditError):
            run_apply_fixes(
                self.db, "s", "der", str(self.input), str(output),
                report_path=str(report),
            )
        # The report was newly created, then had to be withdrawn; the CSV
        # never landed.
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_restore_failure_is_reported_with_original_failure(self) -> None:
        self._propose_overlapping()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        output.write_text("OLD CSV")
        report.write_text("OLD REPORT")
        real_replace = fixes_mod.os.replace

        def flaky(src, dst, *args, **kwargs):
            name = os.path.basename(str(src))
            if str(dst) == str(output) and ".restore." not in name:
                raise OSError("primary replace failure")
            if ".restore." in name:
                raise OSError("restore failure")
            return real_replace(src, dst, *args, **kwargs)

        with patch.object(fixes_mod.os, "replace", flaky):
            with self.assertRaises(AuditError) as ctx:
                run_apply_fixes(
                    self.db, "s", "der", str(self.input), str(output),
                    report_path=str(report),
                )
        self.assertIn("primary replace failure", ctx.exception.message)
        self.assertIn("restore", ctx.exception.message)

    def test_commit_failure_restores_both_outputs_and_rolls_back(self) -> None:
        self._propose_overlapping()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        output.write_text("OLD CSV")
        report.write_text("OLD REPORT")

        class FailingCommitConn(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("simulated commit failure")
                return super().execute(sql, *args, **kwargs)

        real_connect = fixes_mod.sqlite3.connect
        with patch.object(
            fixes_mod.sqlite3, "connect",
            lambda *a, **k: real_connect(*a, factory=FailingCommitConn, **k),
        ):
            with self.assertRaises(AuditError) as ctx:
                run_apply_fixes(
                    self.db, "s", "der", str(self.input), str(output),
                    report_path=str(report),
                )
        self.assertIn("commit", ctx.exception.message)
        self.assertEqual(output.read_text(), "OLD CSV")
        self.assertEqual(report.read_text(), "OLD REPORT")
        self.assertEqual(self._derived_count("der"), 0)

    def test_staging_failure_before_any_replace_leaves_outputs(self) -> None:
        self._propose_overlapping()
        output = Path(self.dir.name) / "fixed.csv"
        report = Path(self.dir.name) / "report.jsonl"
        output.write_text("OLD CSV")
        report.write_text("OLD REPORT")

        def fail_stage(path, payload):
            raise AuditError("simulated stage failure", filename=path)

        with patch.object(fixes_mod, "_stage_file", fail_stage), \
                self.assertRaises(AuditError):
            run_apply_fixes(
                self.db, "s", "der", str(self.input), str(output),
                report_path=str(report),
            )
        self.assertEqual(output.read_text(), "OLD CSV")
        self.assertEqual(report.read_text(), "OLD REPORT")
        self.assertEqual(self._derived_count("der"), 0)

    def _derived_count(self, derived_id: str) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM derived_batches WHERE derived_id = ?",
                (derived_id,),
            ).fetchone()[0]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return 0
            raise
        finally:
            conn.close()

    def test_input_file_is_never_modified(self) -> None:
        self._propose_overlapping()
        before = self.input.read_bytes()
        output = str(Path(self.dir.name) / "fixed.csv")
        self.assertEqual(
            run_apply_fixes(self.db, "s", "der", str(self.input), output), 0
        )
        self.assertEqual(self.input.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
