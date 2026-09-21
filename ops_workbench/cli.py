"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "audit-orders":
        try:
            return run_audit(args.schema, args.input, args.output)
        except AuditError as exc:
            location = f"{exc.filename}: " if exc.filename else ""
            print(
                f"ops-workbench audit-orders: error: {location}{exc.message}",
                file=sys.stderr,
            )
            return 2

    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2  # pragma: no cover
