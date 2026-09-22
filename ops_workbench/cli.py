"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .decisions import DecisionConflictError, run_decide, run_review
from .diff_audits import run_diff
from .fixes import FixConflictError, run_apply_fixes, run_propose_fix
from .orders_audit import AuditError, BatchConflictError, run_audit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-workbench",
        description="Local operations data quality and reconciliation workbench.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    audit = subparsers.add_parser(
        "audit-orders",
        help="audit an orders CSV for field and duplicate/conflict findings",
        description=(
            "Validate an orders CSV against a column-mapping schema and emit "
            "findings as JSON Lines."
        ),
    )
    audit.add_argument(
        "--schema",
        required=True,
        metavar="S",
        help=(
            "UTF-8 JSON object with exactly the keys order_id, sku, qty, "
            "status, updated_at; each value is a non-empty array of candidate "
            "header column names."
        ),
    )
    audit.add_argument("input", metavar="INPUT", help="UTF-8 CSV file to audit")
    audit.add_argument(
        "--output",
        metavar="O",
        default=None,
        help="write the report here (atomically replaced); defaults to stdout",
    )
    audit.add_argument(
        "--db",
        metavar="DB",
        default=None,
        help=(
            "SQLite database used to trace audit batches; must be given "
            "together with --batch"
        ),
    )
    audit.add_argument(
        "--batch",
        metavar="ID",
        default=None,
        help=(
            "store this run under batch ID in --db; an identical stored "
            "batch is returned idempotently, a conflicting one exits 3"
        ),
    )

    diff = subparsers.add_parser(
        "diff-audits",
        help="explain the finding differences between two stored batches",
        description=(
            "Compare the finding sets of batches OLD and NEW stored in the "
            "--db SQLite database and emit added/resolved/changed items as "
            "JSON Lines."
        ),
    )
    diff.add_argument(
        "--db",
        required=True,
        metavar="DB",
        help="SQLite database written by audit-orders --db/--batch",
    )
    diff.add_argument("old", metavar="OLD", help="id of the older batch")
    diff.add_argument("new", metavar="NEW", help="id of the newer batch")
    diff.add_argument(
        "--output",
        metavar="O",
        default=None,
        help="write the diff here (atomically replaced); defaults to stdout",
    )

    decide = subparsers.add_parser(
        "decide",
        help="record a manual disposition for one finding of a stored batch",
        description=(
            "Attach a human decision (confirm, ignore or fix with a reason) "
            "to one finding of a batch stored by audit-orders --db/--batch."
        ),
    )
    decide.add_argument(
        "--db",
        required=True,
        metavar="DB",
        help="SQLite database written by audit-orders --db/--batch",
    )
    decide.add_argument("batch", metavar="BATCH", help="id of the stored batch")
    decide.add_argument(
        "identity",
        metavar="ID",
        help=(
            "JSON identity of the finding as used by diff-audits, e.g. "
            '\'["invalid",2,"qty"]\''
        ),
    )
    decide.add_argument(
        "action",
        metavar="ACTION",
        choices=("confirm", "ignore", "fix"),
        help="manual disposition: confirm, ignore or fix",
    )
    decide.add_argument("reason", metavar="REASON", help="non-empty reason text")

    review = subparsers.add_parser(
        "review-decisions",
        help="replay the decisions of batch OLD against batch NEW",
        description=(
            "Review which manual decisions recorded for batch OLD still hold "
            "against the findings of batch NEW and emit kept/invalid/pending "
            "items as JSON Lines."
        ),
    )
    review.add_argument(
        "--db",
        required=True,
        metavar="DB",
        help="SQLite database written by audit-orders --db/--batch",
    )
    review.add_argument("old", metavar="OLD", help="id of the batch with decisions")
    review.add_argument("new", metavar="NEW", help="id of the batch to review against")
    review.add_argument(
        "--output",
        metavar="O",
        default=None,
        help="write the review here (atomically replaced); defaults to stdout",
    )

    propose = subparsers.add_parser(
        "propose-fix",
        help="attach a validated cell patch to a finding with a fix decision",
        description=(
            "Propose a traceable correction PATCH for one finding of a stored "
            "batch whose recorded decision is 'fix'."
        ),
    )
    propose.add_argument(
        "--db",
        required=True,
        metavar="DB",
        help="SQLite database written by audit-orders --db/--batch",
    )
    propose.add_argument("batch", metavar="BATCH", help="id of the stored batch")
    propose.add_argument(
        "identity",
        metavar="ID",
        help=(
            "JSON identity of the finding as used by diff-audits, e.g. "
            '\'["invalid",2,"qty"]\''
        ),
    )
    propose.add_argument(
        "patch",
        metavar="PATCH",
        help=(
            "non-empty JSON array of [record number, field, new value] "
            "triples; field is one of order_id, sku, qty, status, updated_at"
        ),
    )

    apply = subparsers.add_parser(
        "apply-fixes",
        help="apply all fix proposals of SOURCE to a matching CSV",
        description=(
            "Apply every fix proposal recorded for SOURCE to INPUT (whose "
            "bytes must hash to SOURCE), re-audit the result with the SOURCE "
            "schema, write the fixed CSV and trace it as DERIVED."
        ),
    )
    apply.add_argument(
        "--db",
        required=True,
        metavar="DB",
        help="SQLite database written by audit-orders --db/--batch",
    )
    apply.add_argument("source", metavar="SOURCE", help="id of the source batch")
    apply.add_argument(
        "derived", metavar="DERIVED", help="id under which the fixed run is traced"
    )
    apply.add_argument("input", metavar="INPUT", help="UTF-8 CSV matching SOURCE")
    apply.add_argument(
        "--output",
        required=True,
        metavar="CSV",
        help="write the fixed CSV here (atomically replaced); never INPUT",
    )
    apply.add_argument(
        "--report",
        metavar="R",
        default=None,
        help="write the re-audit JSONL here (atomically replaced); defaults to stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "audit-orders":
        if (args.db is None) != (args.batch is None):
            print(
                "ops-workbench audit-orders: error: --db and --batch must be "
                "given together",
                file=sys.stderr,
            )
            return 2
        try:
            return run_audit(
                args.schema,
                args.input,
                args.output,
                db_path=args.db,
                batch_id=args.batch,
            )
        except BatchConflictError as exc:
            print(f"ops-workbench audit-orders: error: {exc}", file=sys.stderr)
            return 3
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench audit-orders: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    if args.command == "diff-audits":
        try:
            return run_diff(args.db, args.old, args.new, args.output)
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench diff-audits: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    if args.command == "decide":
        try:
            return run_decide(
                args.db, args.batch, args.identity, args.action, args.reason
            )
        except DecisionConflictError as exc:
            print(f"ops-workbench decide: error: {exc}", file=sys.stderr)
            return 3
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench decide: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    if args.command == "review-decisions":
        try:
            return run_review(args.db, args.old, args.new, args.output)
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench review-decisions: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    if args.command == "propose-fix":
        try:
            return run_propose_fix(
                args.db, args.batch, args.identity, args.patch
            )
        except FixConflictError as exc:
            print(f"ops-workbench propose-fix: error: {exc}", file=sys.stderr)
            return 3
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench propose-fix: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    if args.command == "apply-fixes":
        try:
            return run_apply_fixes(
                args.db,
                args.source,
                args.derived,
                args.input,
                args.output,
                args.report,
            )
        except FixConflictError as exc:
            print(f"ops-workbench apply-fixes: error: {exc}", file=sys.stderr)
            return 3
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench apply-fixes: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2  # pragma: no cover
