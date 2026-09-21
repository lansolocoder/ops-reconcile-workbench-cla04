"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from . import audit_orders


def _add_audit_orders(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "audit-orders",
        help="audit an orders CSV for invalid rows and duplicate/conflicting groups",
    )
    parser.add_argument("--schema", required=True, help="UTF-8 JSON schema (path or inline JSON)")
    parser.add_argument("input", help="UTF-8 CSV input file (a leading BOM is accepted)")
    parser.add_argument("--output", help="write the JSONL report here instead of stdout")
    parser.set_defaults(handler=_handle_audit_orders)


def _handle_audit_orders(args: argparse.Namespace) -> int:
    try:
        return audit_orders.run(args.schema, args.input, args.output)
    except audit_orders.AuditError as exc:
        print(f"ops-workbench: error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ops-workbench",
        description="Local operations data quality and reconciliation workbench.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    _add_audit_orders(subparsers)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.handler(args)
