"""Implementation of the ``diff-audits`` subcommand.

Compares two persisted audit batches (see :mod:`ops_workbench.batches`)
and emits JSON Lines explaining the differences:

* ``["added", identity, new_finding]``  — only in NEW
* ``["resolved", identity, old_finding]``  — only in OLD
* ``["changed", identity, old_finding, new_finding]``  — same identity,
  different full finding
* a trailing ``["summary", ...]`` line

The identity of a finding is the part stable across audits:

* ``invalid``   -> ``["invalid", record_no, field]``
* ``duplicate`` -> ``["duplicate", order_id, sku]``
* ``conflict``  -> ``["conflict", order_id, sku]``

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import BinaryIO, TextIO

from .batches import BatchStore
from .orders_audit import AuditError


def finding_identity(finding: list) -> list:
    """Return the stable identity of one finding (without ``summary``)."""
    kind = finding[0]
    if kind == "invalid":
        # ["invalid", record_no, field, raw_value]
        return ["invalid", finding[1], finding[2]]
    # ["duplicate"|"conflict", [order_id, sku], record_numbers]
    order_id, sku = finding[1]
    return [kind, order_id, sku]


def _identity_key(identity: list):
    """Sort key: item-by-item ascending over the JSON identity.

    Elements are strings or ints; compare structurally the way JSON values
    order (numbers before text is irrelevant here because positions are
    fixed per kind, but the heterogeneous case is handled safely).
    """
    return [
        (1, x) if isinstance(x, int) and not isinstance(x, bool) else (0, x)
        for x in identity
    ]


def diff_findings(
    old_findings: list[list],
    old_hash: str,
    new_findings: list[list],
    new_hash: str,
) -> list[list]:
    """Compute the JSON Lines diff body (including the summary line)."""
    old_by_id: dict[str, list] = {}
    new_by_id: dict[str, list] = {}
    for finding in old_findings:
        old_by_id[json.dumps(finding_identity(finding), sort_keys=True)] = finding
    for finding in new_findings:
        new_by_id[json.dumps(finding_identity(finding), sort_keys=True)] = finding

    result: list[list] = []
    added = resolved = changed = 0
    for token in sorted(set(old_by_id) | set(new_by_id),
                        key=lambda t: _identity_key(json.loads(t))):
        identity = json.loads(token)
        old_f = old_by_id.get(token)
        new_f = new_by_id.get(token)
        if old_f is None:
            result.append(["added", identity, new_f])
            added += 1
        elif new_f is None:
            result.append(["resolved", identity, old_f])
            resolved += 1
        elif old_f != new_f:
            result.append(["changed", identity, old_f, new_f])
            changed += 1
        # Identical identity and identical full finding: omitted.

    result.append(
        ["summary", added, resolved, changed, old_hash, new_hash]
    )
    return result


def serialize(lines: list[list]) -> bytes:
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in lines
    )
    return (text + "\n").encode("utf-8")


def _atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path: str | None = None
    try:
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
            )
        except OSError as exc:
            raise AuditError(f"cannot prepare output: {exc}", filename=path)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
        except OSError as exc:
            raise AuditError(f"cannot write output: {exc}", filename=path)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def run_diff(
    db_path: str,
    old_id: str,
    new_id: str,
    output_path: str | None = None,
    stdout: BinaryIO | TextIO | None = None,
) -> int:
    """Load both batches and emit the diff; always exits 0 on success."""
    with BatchStore(db_path, readonly=True) as store:
        # Load both batches before emitting anything: a missing batch must
        # produce no partial results.
        old_hash, _old_schema, old_findings = store.load(old_id)
        new_hash, _new_schema, new_findings = store.load(new_id)
    lines = diff_findings(old_findings, old_hash, new_findings, new_hash)

    payload = serialize(lines)
    if output_path is None:
        if stdout is None:  # pragma: no cover - always injected by main
            import sys

            stdout = sys.stdout
        try:
            if hasattr(stdout, "buffer"):
                stdout.buffer.write(payload)
                stdout.buffer.flush()
            else:  # pragma: no cover - binary stream convenience
                stdout.write(payload)
                stdout.flush()
        except OSError as exc:
            raise AuditError(f"cannot write report: {exc}", filename="<stdout>")
    else:
        _atomic_write(output_path, payload)
    return 0
