"""Command-line entry point."""

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__
from .batches import BatchConflict, BatchMissing, StoreError
from .diff_audits import run_diff
from .orders_audit import AuditError, run_audit


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
            "SQLite database for batch traceability; must be used together "
            "with --batch"
        ),
    )
    audit.add_argument(
        "--batch",
        metavar="ID",
        default=None,
        help="batch id stored in DB; must be used together with --db",
    )

    diff = subparsers.add_parser(
        "diff-audits",
        help="explain the differences between two persisted audit batches",
        description=(
            "Compare two audit batches stored in a SQLite database and emit "
            "added/resolved/changed findings as JSON Lines."
        ),
    )
    diff.add_argument("--db", metavar="DB", required=True, help="SQLite database")
    diff.add_argument("old", metavar="OLD", help="old batch id")
    diff.add_argument("new", metavar="NEW", help="new batch id")
    diff.add_argument(
        "--output",
        metavar="O",
        default=None,
        help="write the diff here (atomically replaced); defaults to stdout",
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
            parser.error("--db and --batch must be given together")
        try:
            return run_audit(
                args.schema,
                args.input,
                args.output,
                db_path=args.db,
                batch_id=args.batch,
            )
        except BatchConflict as exc:
            labels = {
                "hash": "input SHA-256",
                "schema": "parsed schema",
                "findings": "finding set",
            }
            print(
                f"ops-workbench audit-orders: error: batch {exc.batch_id!r} "
                f"already exists with a different {labels[exc.field]}; "
                f"stored={json.dumps(exc.stored, ensure_ascii=False)} "
                f"current={json.dumps(exc.current, ensure_ascii=False)}; "
                f"database and previous output left unchanged",
                file=sys.stderr,
            )
            return 3
        except StoreError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench audit-orders: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2
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
        except BatchMissing as exc:
            print(
                f"ops-workbench diff-audits: error: {exc}",
                file=sys.stderr,
            )
            return 2
        except StoreError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench diff-audits: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench diff-audits: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2  # pragma: no cover
