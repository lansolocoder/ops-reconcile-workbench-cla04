"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .reconcile import ReconcileError, reconcile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-workbench",
        description="Local operations data quality and reconciliation workbench.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Reconcile orders against fulfillments and write a JSONL report.",
    )
    reconcile_parser.add_argument("--orders", required=True, metavar="O",
                                  help="UTF-8 CSV of order lines.")
    reconcile_parser.add_argument("--fulfillments", required=True, metavar="F",
                                  help="UTF-8 CSV of fulfillment lines.")
    reconcile_parser.add_argument("--output", required=True, metavar="R",
                                  help="Destination JSONL report path.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "reconcile":
        try:
            reconcile(args.orders, args.fulfillments, args.output)
        except ReconcileError as exc:
            print(f"reconcile: {exc}", file=sys.stderr)
            return 1
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2
