"""Implementation of the ``diff-audits`` subcommand.

Compares the finding sets of two batches stored by ``audit-orders
--db DB --batch ID`` and explains the differences as JSON Lines.  Only the
Python standard library is used.
"""

from __future__ import annotations

import json
from typing import BinaryIO, TextIO

from .orders_audit import AuditError, emit_report, fetch_batch, serialize


def finding_identity(finding: list) -> list:
    """Stable identity of a finding, independent of its full payload.

    ``invalid`` findings identify by record number and logical field;
    ``duplicate``/``conflict`` findings by type, order id and sku.
    """
    if finding[0] == "invalid":
        return ["invalid", finding[1], finding[2]]
    return [finding[0], finding[1][0], finding[1][1]]


def diff_findings(old_findings: list[list], new_findings: list[list]) -> list[list]:
    """Explain how ``new_findings`` differs from ``old_findings``.

    Returns ``added``/``resolved``/``changed`` items ordered ascending by
    identity, element by element.  Identical findings are omitted.  The
    ``summary`` item is appended by the caller.
    """
    old_by_id = {tuple(finding_identity(f)): f for f in old_findings}
    new_by_id = {tuple(finding_identity(f)): f for f in new_findings}

    items: list[list] = []
    # Identities with the same type tag have homogeneous tail types, so the
    # plain element-wise tuple order is total here.
    for identity in sorted(set(old_by_id) | set(new_by_id)):
        old_finding = old_by_id.get(identity)
        new_finding = new_by_id.get(identity)
        ident = list(identity)
        if old_finding is None:
            items.append(["added", ident, new_finding])
        elif new_finding is None:
            items.append(["resolved", ident, old_finding])
        elif old_finding != new_finding:
            items.append(["changed", ident, old_finding, new_finding])
    return items


def run_diff(
    db_path: str,
    old_id: str,
    new_id: str,
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Diff two stored batches and emit the explanation; return exit code.

    A missing database or batch raises :class:`AuditError` (exit 2) before
    anything is written, so no partial results are produced.
    """
    old = fetch_batch(db_path, old_id)
    if old is None:
        raise AuditError(f"batch {old_id!r} not found", filename=db_path)
    new = fetch_batch(db_path, new_id)
    if new is None:
        raise AuditError(f"batch {new_id!r} not found", filename=db_path)

    old_hash, _, old_findings_json = old
    new_hash, _, new_findings_json = new
    items = diff_findings(json.loads(old_findings_json), json.loads(new_findings_json))

    added = sum(1 for item in items if item[0] == "added")
    resolved = sum(1 for item in items if item[0] == "resolved")
    changed = sum(1 for item in items if item[0] == "changed")
    items.append(["summary", added, resolved, changed, old_hash, new_hash])

    emit_report(serialize(items), output_path, stdout)
    return 0
