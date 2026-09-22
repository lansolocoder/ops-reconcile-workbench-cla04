"""Command-line entry point."""

import argparse
from collections.abc import Sequence

from . import __version__
from .adjust_stock import run_adjust_stock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ops-workbench",
        description="Local operations data quality and reconciliation workbench.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    adjust = subparsers.add_parser(
        "adjust-stock",
        help="apply stock adjustments to a snapshot and write a JSON report",
        description=(
            "Apply the adjustments in ADJUSTMENTS to the stock levels in SNAPSHOT "
            "(both UTF-8 CSV) and atomically write the resulting stock and audit "
            "trail to REPORT (JSON). The whole batch fails if any row is invalid."
        ),
    )
    adjust.add_argument("snapshot", help="snapshot CSV: warehouse,sku,on_hand,updated_at")
    adjust.add_argument(
        "adjustments",
        help="adjustments CSV: id,warehouse,sku,expected,delta,reason,occurred_at",
    )
    adjust.add_argument("report", help="output JSON report path (must differ from inputs)")

    args = parser.parse_args(argv)
    if args.command == "adjust-stock":
        return run_adjust_stock(args.snapshot, args.adjustments, args.report)
    parser.print_help()
    return 0
