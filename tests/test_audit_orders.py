"""End-to-end checks for the ``audit-orders`` command."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

SCHEMA = {
    "order_id": ["oid", "order_id"],
    "sku": ["item", "sku"],
    "qty": ["quantity", "qty"],
    "status": ["state", "status"],
    "updated_at": ["ts", "updated_at"],
}

HEADER = "oid,item,quantity,state,ts,note\n"
TS = "2026-01-02T03:04:05Z"


class AuditOrdersTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ops_workbench", "audit-orders", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.schema_path = self.tmp / "schema.json"
        self.schema_path.write_text(json.dumps(SCHEMA), encoding="utf-8")
        self.input_path = self.tmp / "input.csv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_audit(self, csv_text: str, *, raw: bytes | None = None, output: str | None = None):
        if raw is not None:
            self.input_path.write_bytes(raw)
        else:
            self.input_path.write_text(csv_text, encoding="utf-8")
        args = ["--schema", str(self.schema_path), str(self.input_path)]
        if output is not None:
            args += ["--output", output]
        result = self.invoke(*args)
        lines = []
        if result.stdout:
            lines = [json.loads(line) for line in result.stdout.splitlines()]
        return result, lines

    def test_clean_report_exit_zero(self) -> None:
        csv_text = HEADER + f"A1,W,3,open,{TS},x\nA2,W,2,cancelled,{TS},y\n"
        result, lines = self.run_audit(csv_text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0], "summary")
        self.assertEqual(lines[0][1], 2)
        self.assertEqual(lines[0][2], 0)
        self.assertEqual(
            lines[0][3],
            hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
        )

    def test_bom_and_extra_columns_and_whitespace_trimming(self) -> None:
        csv_text = HEADER + f" A1 , W ,3, open ,{TS}, x \n"
        raw = b"\xef\xbb\xbf" + csv_text.encode("utf-8")
        result, lines = self.run_audit("", raw=raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines[-1][3], hashlib.sha256(raw).hexdigest())

    def test_duplicate_without_conflict(self) -> None:
        csv_text = HEADER + f"A1,W,3,open,{TS},x\nA1,W,3,open,{TS},y\n"
        result, lines = self.run_audit(csv_text)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(lines[:-1], [["duplicate", ["A1", "W"], [2, 3]]])
        self.assertEqual(lines[-1], ["summary", 2, 1, lines[-1][3]])

    def test_duplicate_and_conflict_conflict_sorted_first(self) -> None:
        csv_text = HEADER + (
            f"A1,W,3,open,{TS},x\n"
            f"A1,W,4,open,{TS},y\n"
            f"B2,W,3,open,{TS},z\n"
            f"B2,W,3,cancelled,{TS},w\n"
        )
        result, lines = self.run_audit(csv_text)
        self.assertEqual(result.returncode, 1)
        # Group B2 has min record 4, comes after group A1 (min record 2);
        # within one group conflict sorts before duplicate.
        self.assertEqual(
            lines[:-1],
            [
                ["conflict", ["A1", "W"], [2, 3]],
                ["duplicate", ["A1", "W"], [2, 3]],
                ["conflict", ["B2", "W"], [4, 5]],
                ["duplicate", ["B2", "W"], [4, 5]],
            ],
        )
        self.assertEqual(lines[-1][:3], ["summary", 4, 4])

    def test_invalid_rows_fields_sorted_and_raw_value_reported(self) -> None:
        csv_text = HEADER + (
            f" ,W,3,open,{TS},blank-oid\n"                       # record 2: order_id
            f"A2,W,0,open,{TS},zero-qty\n"                       # record 3: qty
            f"A3, ,3,open,{TS},blank-sku\n"                      # record 4: sku
            f"A4,W,3,pending,{TS},bad-status\n"                  # record 5: status
            f"A5,W,3,open,2026-01-02T03:04Z,minute-precision\n"  # record 6: updated_at
            f"A6,W,12,shipped,not-a-time,both\n"                 # record 7: status+updated_at
        )
        result, lines = self.run_audit(csv_text)
        self.assertEqual(result.returncode, 1, result.stderr)
        findings = lines[:-1]
        self.assertEqual(
            findings,
            [
                ["invalid", 2, "order_id", " "],
                ["invalid", 3, "qty", "0"],
                ["invalid", 4, "sku", " "],
                ["invalid", 5, "status", "pending"],
                ["invalid", 6, "updated_at", "2026-01-02T03:04Z"],
                ["invalid", 7, "status", "shipped"],
                ["invalid", 7, "updated_at", "not-a-time"],
            ],
        )
        self.assertEqual(lines[-1][:3], ["summary", 6, 7])

    def test_invalid_rows_are_not_grouped(self) -> None:
        # The second A1 row has a bad qty, so no duplicate finding is raised.
        csv_text = HEADER + f"A1,W,3,open,{TS},x\nA1,W,xx,open,{TS},y\n"
        result, lines = self.run_audit(csv_text)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], ["invalid", 3, "qty", "xx"])

    def test_record_numbers_count_header_as_one_and_skip_blank_lines(self) -> None:
        csv_text = (
            HEADER
            + f"A1,W,3,open,{TS},x\n"
            + "\n"
            + f"A1,W,3,open,{TS},y\n"
        )
        result, lines = self.run_audit(csv_text)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(lines[0], ["duplicate", ["A1", "W"], [2, 4]])
        self.assertEqual(lines[-1][1], 2)  # 2 data rows, blank line excluded

    def test_output_file_atomic_replace_on_success(self) -> None:
        output_path = self.tmp / "report.jsonl"
        output_path.write_text("OLD CONTENT", encoding="utf-8")
        csv_text = HEADER + f"A1,W,3,open,{TS},x\n"
        result, _ = self.run_audit(csv_text, output=str(output_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        written = output_path.read_text(encoding="utf-8")
        report_lines = [json.loads(line) for line in written.splitlines()]
        self.assertEqual(report_lines[-1][0], "summary")
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith("report.jsonl.tmp")]
        self.assertEqual(leftovers, [])

    def test_output_failure_keeps_old_file_and_cleans_temp(self) -> None:
        if os.geteuid() == 0 if hasattr(os, "geteuid") else False:
            self.skipTest("root bypasses directory permissions")
        locked = self.tmp / "locked"
        locked.mkdir()
        output_path = locked / "report.jsonl"
        output_path.write_text("OLD CONTENT", encoding="utf-8")
        os.chmod(locked, 0o000)
        try:
            csv_text = HEADER + f"A1,W,3,open,{TS},x\n"
            result, _ = self.run_audit(csv_text, output=str(output_path))
        finally:
            os.chmod(locked, 0o755)
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(output_path), result.stderr)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "OLD CONTENT")
        self.assertEqual(list(locked.iterdir()), [output_path])

    def test_fatal_inputs_do_not_touch_output_file(self) -> None:
        output_path = self.tmp / "report.jsonl"
        output_path.write_text("OLD CONTENT", encoding="utf-8")

        # Invalid UTF-8 input.
        self.input_path.write_bytes(b"oid,item\n\xff\xfe\n")
        result = self.invoke(
            "--schema", str(self.schema_path), str(self.input_path),
            "--output", str(output_path),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(self.input_path), result.stderr)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "OLD CONTENT")

    def test_bad_schema_messages(self) -> None:
        bad_schemas = [
            "not json at all",
            json.dumps({k: v for k, v in SCHEMA.items() if k != "qty"}),
            json.dumps({**SCHEMA, "extra": ["x"]}),
            json.dumps({**SCHEMA, "qty": []}),
            json.dumps({**SCHEMA, "qty": ["ok", ""]}),
            json.dumps({**SCHEMA, "qty": "not-a-list"}),
        ]
        csv_text = HEADER + f"A1,W,3,open,{TS},x\n"
        self.input_path.write_text(csv_text, encoding="utf-8")
        for index, schema_text in enumerate(bad_schemas):
            with self.subTest(index=index):
                schema_path = self.tmp / f"bad{index}.json"
                schema_path.write_text(schema_text, encoding="utf-8")
                result = self.invoke(
                    "--schema", str(schema_path), str(self.input_path)
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(str(schema_path), result.stderr)

    def test_inline_schema_argument(self) -> None:
        csv_text = HEADER + f"A1,W,3,open,{TS},x\n"
        self.input_path.write_text(csv_text, encoding="utf-8")
        result = self.invoke(
            "--schema", json.dumps(SCHEMA), str(self.input_path)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith("]"))

    def test_header_errors_are_fatal(self) -> None:
        cases = {
            "empty_file": b"",
            "empty_header": b"\n",
            "duplicate_header": b"oid,oid,quantity,state,ts,note\n",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                result, _ = self.run_audit("", raw=raw)
                self.assertEqual(result.returncode, 2)
                self.assertIn("input", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_schema_column_resolution_errors(self) -> None:
        csv_text = HEADER + f"A1,W,3,open,{TS},x\n"
        self.input_path.write_text(csv_text, encoding="utf-8")
        bad_schemas = [
            # No header matches any qty candidate.
            {**SCHEMA, "qty": ["nope"]},
            # Two headers match order_id candidates.
            {**SCHEMA, "sku": ["oid"]},
        ]
        for index, schema in enumerate(bad_schemas):
            with self.subTest(index=index):
                schema_path = self.tmp / f"resolve{index}.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                result = self.invoke(
                    "--schema", str(schema_path), str(self.input_path)
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("input", result.stderr)

    def test_missing_input_file(self) -> None:
        missing = self.tmp / "does-not-exist.csv"
        result = self.invoke("--schema", str(self.schema_path), str(missing))
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(missing), result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
