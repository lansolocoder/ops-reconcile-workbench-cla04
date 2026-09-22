"""Command-line entry point."""

import argparse
from collections.abc import Sequence

from . import __version__
from .adjust_stock import run_adjust_stock
from .replay_stock import run_replay_stock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ops-workbench",
        description="Local operations data quality and reconciliation workbench.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(title="commands", metavar="COMMAND")

    adjust_parser = subparsers.add_parser(
        "adjust-stock",
        help="apply stock adjustments from CSV files and write a JSON report",
        description=(
            "Validate a stock snapshot and an adjustments CSV, apply every "
            "adjustment as one atomic batch, and write a JSON report."
        ),
    )
    adjust_parser.add_argument("snapshot", help="UTF-8 CSV snapshot path")
    adjust_parser.add_argument("adjustments", help="UTF-8 CSV adjustments path")
    adjust_parser.add_argument("report", help="output JSON report path")
    adjust_parser.set_defaults(handler=run_adjust_stock)

    replay_parser = subparsers.add_parser(
        "replay-stock",
        help="merge adjustments into a trusted JSON report and write a new report",
        description=(
            "Take a trusted JSON report (from adjust-stock or replay-stock) "
            "as the inventory starting point, skip adjustments already "
            "recorded in its audit, apply the remaining ones as one atomic "
            "batch, and write a new JSON report."
        ),
    )
    replay_parser.add_argument("base", help="UTF-8 JSON base report path")
    replay_parser.add_argument("adjustments", help="UTF-8 CSV adjustments path")
    replay_parser.add_argument("report", help="output JSON report path")
    replay_parser.set_defaults(handler=run_replay_stock)

    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
