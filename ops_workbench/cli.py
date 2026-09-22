"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .reconcile import ReconcileError, reconcile_files, write_jsonl


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-workbench",
        description="Local operations data quality and reconciliation workbench.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="reconcile order lines against fulfillment shipments",
        description=(
            "Reconcile UTF-8 comma CSV order and fulfillment files and write "
            "a JSON Lines report."
        ),
    )
    reconcile_parser.add_argument(
        "--orders",
        required=True,
        metavar="O",
        help="orders CSV: order_id,line_id,sku,ordered_qty,status",
    )
    reconcile_parser.add_argument(
        "--fulfillments",
        required=True,
        metavar="F",
        help="fulfillments CSV: shipment_id,order_id,line_id,sku,shipped_qty",
    )
    reconcile_parser.add_argument(
        "--output",
        required=True,
        metavar="R",
        help="path of the JSON Lines report to write",
    )

    return parser


def _run_reconcile(args: argparse.Namespace) -> int:
    try:
        records = reconcile_files(args.orders, args.fulfillments)
        write_jsonl(args.output, records)
    except ReconcileError as exc:
        print(f"reconcile: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        detail = exc.strerror or str(exc)
        print(
            f"reconcile: {args.output}: cannot write report: {detail}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "reconcile":
        return _run_reconcile(args)

    parser.print_help()
    return 0
