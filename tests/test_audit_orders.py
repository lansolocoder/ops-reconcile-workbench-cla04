"""Checks for the ``audit-orders`` subcommand."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from ops_workbench.orders_audit import AuditError, audit, run_audit

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


class AuditCoreTests(unittest.TestCase):
    def audit(self, csv_text: str, schema: str = SCHEMA) -> tuple[list[list], int]:
        data = csv_text.encode("utf-8")
        findings, row_count = audit(data, schema, "orders.csv")
        self.assertEqual(
            findings[-1],
            ["summary", row_count, len(findings) - 1, hashlib.sha256(data).hexdigest()],
        )
        return findings, row_count

    def test_clean_file_has_no_findings(self) -> None:
        findings, n = self.audit(
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05+08:00,x\n"
            + "A2,S2,9,cancelled,2024-01-02T03:04:05Z,y\n"
        )
        self.assertEqual(n, 2)
        self.assertEqual(findings[:-1], [])

    def test_bom_is_accepted(self) -> None:
        data = b"\xef\xbb\xbf" + HEADER.encode() + b"A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
        findings, n = audit(data, SCHEMA, "orders.csv")
        self.assertEqual(n, 1)
        self.assertEqual(findings[:-1], [])

    def test_trimmed_identifiers_group_as_duplicate(self) -> None:
        findings, _ = self.audit(
            HEADER
            + " A1 , S1 ,3,open,2024-01-02T03:04:05Z,x\n"
            + "A1,S1,3,open,2024-01-02T03:04:05Z,y\n"
        )
        self.assertEqual(findings[0], ["duplicate", ["A1", "S1"], [2, 3]])

    def test_conflict_reports_both_duplicate_and_conflict(self) -> None:
        findings, _ = self.audit(
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
            + "A1,S1,4,open,2024-01-02T03:04:05Z,y\n"
        )
        body = findings[:-1]
        # Type text sorts lexicographically, so "conflict" precedes
        # "duplicate" when both involve the same smallest record number.
        self.assertEqual(body[0], ["conflict", ["A1", "S1"], [2, 3]])
        self.assertEqual(body[1], ["duplicate", ["A1", "S1"], [2, 3]])
        self.assertEqual([item[0] for item in body], ["conflict", "duplicate"])

    def test_status_and_timestamp_conflicts(self) -> None:
        for col, second in [
            ("status", "cancelled"),
            ("updated_at", "2024-01-02T03:04:06Z"),
        ]:
            with self.subTest(col=col):
                row1 = "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
                cells = ["A1", "S1", "3", "open", "2024-01-02T03:04:05Z"]
                cells[{"status": 3, "updated_at": 4}[col]] = second
                row2 = ",".join(cells) + ",y\n"
                findings, _ = self.audit(HEADER + row1 + row2)
                kinds = [item[0] for item in findings[:-1]]
                self.assertEqual(kinds, ["conflict", "duplicate"])

    def test_invalid_rows_are_excluded_from_groups(self) -> None:
        findings, n = self.audit(
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
            + "A1,S1,0,open,2024-01-02T03:04:05Z,y\n"
        )
        self.assertEqual(n, 2)
        self.assertEqual(
            [item[0] for item in findings[:-1]],
            ["invalid"],
        )
        self.assertEqual(findings[0], ["invalid", 3, "qty", "0"])

    def test_field_validation_rules(self) -> None:
        cases = [
            ("qty", "0", False),
            ("qty", "01", False),
            ("qty", "-3", False),
            ("qty", "3 ", False),
            ("qty", "10", True),
            ("status", "OPEN", False),
            ("status", "open", True),
            ("status", "cancelled", True),
            ("updated_at", "2024-01-02T03:04:05Z", True),
            ("updated_at", "2024-01-02T03:04:05+08:00", True),
            ("updated_at", "2024-01-02T03:04:05-05:30", True),
            ("updated_at", "2024-01-02 03:04:05Z", False),
            ("updated_at", "2024-01-02T03:04:05", False),
            ("updated_at", "2024-01-02T03:04:05+0800", False),
            ("updated_at", "2024-01-02T03:04:05.5Z", False),
            ("updated_at", "2024-01-02T03:04Z", False),
            ("updated_at", "20240102T030405Z", False),
            ("updated_at", "2024-13-02T03:04:05Z", False),
            ("updated_at", "2024-02-30T03:04:05Z", False),
            ("updated_at", "2024-01-02T24:00:00Z", False),
            ("updated_at", "2024-01-02T03:04:60Z", False),
            ("updated_at", "2024-01-02T03:04:05+24:00", False),
        ]
        row_template = {
            "order_id": "A1",
            "sku": "S1",
            "qty": "3",
            "status": "open",
            "updated_at": "2024-01-02T03:04:05Z",
        }
        for field, value, valid in cases:
            with self.subTest(field=field, value=value):
                row = dict(row_template)
                row[field] = value
                csv_text = HEADER + ",".join(
                    row[k] for k in ("order_id", "sku", "qty", "status", "updated_at")
                ) + ",x\n"
                findings, _ = self.audit(csv_text)
                invalid = [f for f in findings[:-1] if f[0] == "invalid"]
                self.assertEqual([f[2] for f in invalid], [] if valid else [field])

    def test_empty_identifier_reports_raw_value(self) -> None:
        findings, _ = self.audit(
            HEADER + "  ,S1,3,open,2024-01-02T03:04:05Z,x\n"
        )
        self.assertEqual(findings[0], ["invalid", 2, "order_id", "  "])

    def test_record_numbers_account_for_blank_lines_and_sort_order(self) -> None:
        csv_text = (
            HEADER
            + "B1,S9,3,open,2024-01-02T03:04:05Z,x\n"
            + "\n"
            + "B1,S9,3,open,2024-01-02T03:04:05Z,y\n"
            + "A1,S9,0,open,2024-01-02T03:04:05Z,z\n"
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 3)
        body = findings[:-1]
        # Findings sort by smallest involved record number: the duplicate
        # group spans records 2-4, so it precedes the record-4 invalid item.
        self.assertEqual(body[0], ["duplicate", ["B1", "S9"], [2, 4]])
        self.assertEqual(body[1], ["invalid", 5, "qty", "0"])

    def test_extra_columns_and_alternative_header_names(self) -> None:
        schema = json.dumps({
            "order_id": ["id"],
            "sku": ["item", "sku"],
            "qty": ["quantity"],
            "status": ["state"],
            "updated_at": ["ts"],
        })
        header = "id,item,quantity,state,ts,extra\n"
        csv_text = header + "A1,S1,3,open,2024-01-02T03:04:05Z,keep\n"
        findings, n = audit(csv_text.encode(), schema, "orders.csv")
        self.assertEqual(n, 1)
        self.assertEqual(findings[:-1], [])

    def test_invalid_fields_within_a_row_are_name_ordered(self) -> None:
        # sku, qty, status and updated_at all fail (columns are shuffled);
        # output follows the lexicographic order of the logical field names,
        # neither the column order nor the fixed logical-field order.
        header = "oid,updated_at,status,qty,sku\n"
        csv_text = header + "A1,bad-time,nope,0,\n"
        findings, _ = audit(
            csv_text.encode(),
            json.dumps({
                "order_id": ["oid"], "sku": ["sku"], "qty": ["qty"],
                "status": ["status"], "updated_at": ["updated_at"],
            }),
            "orders.csv",
        )
        self.assertEqual(
            [f[2] for f in findings[:-1]],
            ["qty", "sku", "status", "updated_at"],
        )
        self.assertEqual(findings[0], ["invalid", 2, "qty", "0"])
        self.assertEqual(findings[1], ["invalid", 2, "sku", ""])
        self.assertEqual(findings[-2], ["invalid", 2, "updated_at", "bad-time"])

    def test_conflict_sorts_before_duplicate_for_same_records(self) -> None:
        findings, _ = self.audit(
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
            + "A1,S1,4,open,2024-01-02T03:04:05Z,y\n"
        )
        self.assertEqual([f[0] for f in findings[:-1]], ["conflict", "duplicate"])

    def test_whitespace_only_lines_are_skipped_but_counted_in_record_numbers(self) -> None:
        csv_text = (
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
            + "   \n"
            + "A1,S1,3,open,2024-01-02T03:04:05Z,y\n"
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 2)
        self.assertEqual(findings[0], ["duplicate", ["A1", "S1"], [2, 4]])

    def test_invalid_utf8_is_fatal(self) -> None:
        data = HEADER.encode() + b"A1,\xff,3,open,2024-01-02T03:04:05Z,x\n"
        with self.assertRaises(AuditError) as ctx:
            audit(data, SCHEMA, "orders.csv")
        self.assertEqual(ctx.exception.filename, "orders.csv")
        self.assertIn("UTF-8", ctx.exception.message)

    def test_malformed_csv_is_fatal(self) -> None:
        with self.assertRaises(AuditError):
            audit((HEADER + '"unterminated,xyz\n').encode(), SCHEMA, "orders.csv")

    def test_ragged_row_is_fatal(self) -> None:
        with self.assertRaisesRegex(AuditError, "record 2"):
            audit((HEADER + "A1,S1,3,open\n").encode(), SCHEMA, "orders.csv")

    def test_empty_and_duplicate_headers_are_fatal(self) -> None:
        with self.assertRaises(AuditError):
            audit(b"a,,c\nx,y,z\n", SCHEMA, "orders.csv")
        with self.assertRaises(AuditError):
            audit(b"a,a\nx,y\n", SCHEMA, "orders.csv")
        with self.assertRaises(AuditError):
            audit(b"", SCHEMA, "orders.csv")

    def test_schema_validation(self) -> None:
        bad_schemas = [
            "not json",
            "[]",
            "{}",
            json.dumps({k: ["x"] for k in
                        ("order_id", "sku", "qty", "status")}),  # missing key
            json.dumps({**{k: ["x"] for k in
                           ("order_id", "sku", "qty", "status", "updated_at")},
                        "extra": ["y"]}),
            json.dumps({**{k: ["x"] for k in
                           ("order_id", "sku", "qty", "status", "updated_at")},
                        "qty": []}),
            json.dumps({**{k: ["x"] for k in
                           ("order_id", "sku", "qty", "status", "updated_at")},
                        "qty": ["x", ""]}),
            json.dumps({**{k: ["x"] for k in
                           ("order_id", "sku", "qty", "status", "updated_at")},
                        "qty": [4]}),
        ]
        for bad in bad_schemas:
            with self.subTest(bad=bad):
                with self.assertRaises(AuditError):
                    audit((HEADER + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n").encode(),
                          bad, "orders.csv")

    def test_column_resolution_failures(self) -> None:
        csv_text = HEADER + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
        # No candidate hits for sku.
        with self.assertRaisesRegex(AuditError, "sku"):
            audit(csv_text.encode(), json.dumps({
                "order_id": ["oid"], "sku": ["nope"], "qty": ["qty"],
                "status": ["status"], "updated_at": ["updated_at"],
            }), "orders.csv")
        # Two candidates hit (oid and order_id both present via header "oid"?
        # build a header containing both candidate names).
        double_header = "oid,order_id,sku,qty,status,updated_at\n"
        with self.assertRaisesRegex(AuditError, "multiple"):
            audit((double_header + "A,B,S1,3,open,2024-01-02T03:04:05Z\n").encode(),
                  SCHEMA, "orders.csv")
        # Two fields resolve to the same column (cross-listed candidate).
        overlap = json.dumps({
            "order_id": ["oid"], "sku": ["sku"], "qty": ["qty"],
            "status": ["qty"], "updated_at": ["updated_at"],
        })
        with self.assertRaisesRegex(AuditError, "distinct"):
            audit(csv_text.encode(), overlap, "orders.csv")

    def test_quoted_embedded_newline_does_not_increment_record_number(self) -> None:
        # The note field spans two physical lines inside quotes; it is one
        # logical record, so the next data row is record 3.
        csv_text = (
            HEADER
            + '"A1",S1,3,open,2024-01-02T03:04:05Z,"line one\nline two"\n'
            + "A1,S1,3,open,2024-01-02T03:04:05Z,y\n"
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 2)
        self.assertEqual(findings[0], ["duplicate", ["A1", "S1"], [2, 3]])

    def test_embedded_newline_in_invalid_cell_keeps_logical_number(self) -> None:
        csv_text = (
            HEADER
            + 'A1,S1,3,open,"not\na timestamp",x\n'
            + "A1,S1,3,open,2024-01-02T03:04:05Z,y\n"
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 2)
        self.assertEqual(
            findings[0], ["invalid", 2, "updated_at", "not\na timestamp"]
        )

    def test_blank_lines_occupy_logical_record_numbers(self) -> None:
        csv_text = (
            HEADER
            + "\n"
            + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
            + "   \t \n"
            + "A1,S1,3,open,2024-01-02T03:04:05Z,y\n"
            + "\n"
        )
        findings, n = self.audit(csv_text)
        # Two blank/whitespace records consume numbers 2 and 4; data rows
        # are records 3 and 5 and count as two data rows.
        self.assertEqual(n, 2)
        self.assertEqual(findings[0], ["duplicate", ["A1", "S1"], [3, 5]])

    def test_embedded_newline_followed_by_blank_line_numbering(self) -> None:
        csv_text = (
            HEADER
            + 'A1,S1,3,open,2024-01-02T03:04:05Z,"a\nb"\n'
            + "\n"
            + "A1,S1,3,open,2024-01-02T03:04:05Z,y\n"
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 2)
        # Multiline record is 2, blank is 3, second data row is 4.
        self.assertEqual(findings[0], ["duplicate", ["A1", "S1"], [2, 4]])

    def test_mixed_row_and_group_findings_sort_by_min_record(self) -> None:
        csv_text = (
            HEADER
            + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n"
            + "A1,S1,4,open,2024-01-02T03:04:05Z,y\n"  # conflict group 2-3
            + "B2,S9,0,open,2024-01-02T03:04:05Z,z\n"  # invalid at record 4
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 3)
        body = findings[:-1]
        self.assertEqual(
            body,
            [
                ["conflict", ["A1", "S1"], [2, 3]],
                ["duplicate", ["A1", "S1"], [2, 3]],
                ["invalid", 4, "qty", "0"],
            ],
        )

    def test_invalid_at_first_member_precedes_group_findings(self) -> None:
        # An invalid finding at record 2 sorts after the group whose smallest
        # member is also record 2 only by type text: "conflict"/"duplicate"
        # both precede "invalid".  Rows that are invalid never join groups, so
        # the group below is formed from records 3 and 4 instead.
        csv_text = (
            HEADER
            + "A1,S1,0,open,2024-01-02T03:04:05Z,bad-row\n"  # record 2 invalid
            + "C3,S8,5,open,2024-01-02T03:04:05Z,p\n"      # record 3
            + "C3,S8,6,open,2024-01-02T03:04:05Z,q\n"      # record 4
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 3)
        body = findings[:-1]
        self.assertEqual(
            body,
            [
                ["invalid", 2, "qty", "0"],
                ["conflict", ["C3", "S8"], [3, 4]],
                ["duplicate", ["C3", "S8"], [3, 4]],
            ],
        )

    def test_multiple_multiline_groups(self) -> None:
        # Two duplicate groups whose members contain quoted newlines; record
        # numbers stay logical and each group reports duplicate + conflict.
        csv_text = (
            HEADER
            + 'A1,S1,3,open,2024-01-02T03:04:05Z,"g1 first\nnote"\n'  # rec 2
            + 'A1,S1,4,open,2024-01-02T03:04:05Z,"g1 second\nnote"\n'  # rec 3
            + "\n"  # rec 4: skipped, still numbered
            + 'B2,S2,7,open,2024-01-02T03:04:05Z,"g2 first"\n'  # rec 5
            + '"B2",S2,8,open,2024-01-02T03:04:05Z,"g2\nsecond"\n'  # rec 6
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 4)
        body = findings[:-1]
        self.assertEqual(
            body,
            [
                ["conflict", ["A1", "S1"], [2, 3]],
                ["duplicate", ["A1", "S1"], [2, 3]],
                ["conflict", ["B2", "S2"], [5, 6]],
                ["duplicate", ["B2", "S2"], [5, 6]],
            ],
        )

    def test_multiline_group_without_conflict_reports_duplicate_only(self) -> None:
        csv_text = (
            HEADER
            + 'A1,S1,3,open,2024-01-02T03:04:05Z,"same\nsame"\n'
            + 'A1,S1,3,open,2024-01-02T03:04:05Z,"also\nsame"\n'
        )
        findings, n = self.audit(csv_text)
        self.assertEqual(n, 2)
        self.assertEqual(
            findings[:-1], [["duplicate", ["A1", "S1"], [2, 3]]]
        )


class RunAuditFileTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.input = Path(self.dir.name) / "orders.csv"
        self.input.write_bytes(
            (HEADER + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n").encode()
        )

    def test_stdout_clean_exit_zero(self) -> None:
        out = io.BytesIO()
        code = run_audit(SCHEMA, str(self.input), stdout=out)
        self.assertEqual(code, 0)
        lines = parse_lines(out.getvalue().decode())
        self.assertEqual(lines[-1][0], "summary")
        self.assertEqual(lines[-1][1], 1)
        self.assertEqual(lines[-1][2], 0)

    def test_stdout_findings_exit_one(self) -> None:
        self.input.write_bytes((HEADER + "A1,S1,0,open,2024-01-02T03:04:05Z,x\n").encode())
        out = io.BytesIO()
        code = run_audit(SCHEMA, str(self.input), stdout=out)
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().endswith(b"\n"))

    def test_missing_input_is_fatal(self) -> None:
        with self.assertRaises(AuditError):
            run_audit(SCHEMA, str(Path(self.dir.name) / "missing.csv"))

    def test_output_is_atomically_replaced(self) -> None:
        output = Path(self.dir.name) / "report.jsonl"
        output.write_text("OLD CONTENT")
        code = run_audit(SCHEMA, str(self.input), str(output))
        self.assertEqual(code, 0)
        lines = parse_lines(output.read_text())
        self.assertEqual(lines[-1][0], "summary")
        leftovers = [
            p.name for p in Path(self.dir.name).iterdir() if p.name.startswith(".report")
        ]
        self.assertEqual(leftovers, [])

    def test_failed_write_preserves_existing_output(self) -> None:
        # If O names a directory, the temp file is created but os.replace
        # fails (EISDIR); the directory must remain and the temp file is
        # cleaned up.
        output = Path(self.dir.name) / "report.jsonl"
        output.mkdir()
        before = set(os.listdir(self.dir.name))
        with self.assertRaises(AuditError):
            run_audit(SCHEMA, str(self.input), str(output))
        after = set(os.listdir(self.dir.name))
        self.assertEqual(before, after)
        self.assertTrue(output.is_dir())


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.input = Path(self.dir.name) / "orders.csv"
        self.input.write_bytes(
            (HEADER + "A1,S1,3,open,2024-01-02T03:04:05Z,x\n").encode()
        )

    def test_existing_cli_behaviour_preserved(self) -> None:
        for arguments in [(), ("--help",)]:
            with self.subTest(arguments=arguments):
                result = run_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--help", result.stdout)
                self.assertIn("--version", result.stdout)
                self.assertEqual(result.stderr, "")
        version = run_cli("--version")
        self.assertEqual(version.stdout.strip(), "ops-workbench 0.1.0")
        unknown = run_cli("--unknown-option")
        self.assertNotEqual(unknown.returncode, 0)

    def test_subcommand_exit_codes(self) -> None:
        clean = run_cli("audit-orders", "--schema", SCHEMA, str(self.input))
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(clean.stderr, "")

        bad = Path(self.dir.name) / "bad.csv"
        bad.write_bytes((HEADER + "A1,S1,0,open,2024-01-02T03:04:05Z,x\n").encode())
        result = run_cli("audit-orders", "--schema", SCHEMA, str(bad))
        self.assertEqual(result.returncode, 1)

        fatal = run_cli(
            "audit-orders", "--schema", "not-json", str(self.input)
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fatal.returncode, 2)
        self.assertIn("--schema", fatal.stderr)
        self.assertEqual(fatal.stdout, "")

        missing = run_cli(
            "audit-orders", "--schema", SCHEMA, str(Path(self.dir.name) / "nope.csv")
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("nope.csv", missing.stderr)
        self.assertEqual(missing.stdout, "")

    def test_output_flag_writes_file(self) -> None:
        output = Path(self.dir.name) / "out.jsonl"
        result = run_cli(
            "audit-orders", "--schema", SCHEMA, str(self.input),
            "--output", str(output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        lines = parse_lines(output.read_text())
        self.assertEqual(lines[-1][0], "summary")


if __name__ == "__main__":
    unittest.main()
