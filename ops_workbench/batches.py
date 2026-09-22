"""SQLite-backed batch traceability for order audits.

A batch persists, under a caller-supplied id, the SHA-256 of the audited
input bytes, the resolved schema (the parsed mapping) and the finding set
(all findings except the trailing ``summary`` item).  Re-running the same
batch id is idempotent when hash, schema and findings are identical; any
difference is reported as a :class:`BatchConflict` and leaves both the
database and previously written outputs untouched.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import quote


def _quote_uri(path: str) -> str:
    """Percent-encode a filesystem path for use in a SQLite ``file:`` URI."""
    return quote(path, safe="/")


class BatchConflict(Exception):
    """A stored batch exists with the same id but different content."""

    def __init__(self, batch_id: str, field: str, stored: object, current: object):
        self.batch_id = batch_id
        self.field = field
        self.stored = stored
        self.current = current
        labels = {
            "hash": "input SHA-256",
            "schema": "parsed schema",
            "findings": "finding set",
        }
        super().__init__(
            f"batch {batch_id!r} already exists with a different {labels[field]}"
        )


class BatchStore:
    """Small wrapper around a SQLite database holding audit batches."""

    def __init__(self, path: str, *, readonly: bool = False):
        self.path = path
        self.readonly = readonly
        try:
            if readonly:
                # Read-only URI mode never creates a missing database file.
                self.conn = sqlite3.connect(
                    f"file:{_quote_uri(path)}?mode=ro", uri=True
                )
            else:
                self.conn = sqlite3.connect(path)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database: {exc}", filename=path)
        if not readonly:
            self._create_tables()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "BatchStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _create_tables(self) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_batches (
                        batch_id    TEXT PRIMARY KEY,
                        input_hash  TEXT NOT NULL,
                        schema_json TEXT NOT NULL,
                        findings    TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as exc:
            raise StoreError(
                f"cannot initialise database: {exc}", filename=self.path
            )

    def save_or_verify(
        self,
        batch_id: str,
        input_hash: str,
        schema: dict[str, list[str]],
        findings: list[list],
    ) -> bool:
        """Persist a batch, or verify an existing one.

        Returns ``True`` when a new batch was stored, ``False`` when an
        existing identical batch was found.  Raises
        :class:`BatchConflict` when the id exists but the hash, schema or
        finding set differs; in that case nothing is written.
        """
        schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        findings_text = json.dumps(findings, ensure_ascii=False, sort_keys=True)
        try:
            with self.conn:
                cur = self.conn.execute(
                    "SELECT input_hash, schema_json, findings "
                    "FROM audit_batches WHERE batch_id = ?",
                    (batch_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    stored_hash, stored_schema, stored_findings = row
                    if stored_hash != input_hash:
                        field, stored, current = "hash", stored_hash, input_hash
                    elif stored_schema != schema_text:
                        field = "schema"
                        stored = json.loads(stored_schema)
                        current = schema
                    elif stored_findings != findings_text:
                        field = "findings"
                        stored = json.loads(stored_findings)
                        current = findings
                    else:
                        return False
                    raise BatchConflict(batch_id, field, stored, current)

                self.conn.execute(
                    "INSERT INTO audit_batches "
                    "(batch_id, input_hash, schema_json, findings) "
                    "VALUES (?, ?, ?, ?)",
                    (batch_id, input_hash, schema_text, findings_text),
                )
                return True
        except BatchConflict:
            raise
        except sqlite3.Error as exc:
            raise StoreError(f"cannot write batch: {exc}", filename=self.path)

    def delete(self, batch_id: str) -> None:
        """Remove ``batch_id``; used to compensate a failed report write."""
        try:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM audit_batches WHERE batch_id = ?", (batch_id,)
                )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot delete batch: {exc}", filename=self.path)

    def load(self, batch_id: str) -> tuple[str, dict[str, list[str]], list[list]]:
        """Return ``(hash, schema, findings)`` stored for ``batch_id``.

        Raises :class:`BatchMissing` when no such batch exists.
        """
        try:
            cur = self.conn.execute(
                "SELECT input_hash, schema_json, findings "
                "FROM audit_batches WHERE batch_id = ?",
                (batch_id,),
            )
            row = cur.fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"cannot read batch: {exc}", filename=self.path)
        if row is None:
            raise BatchMissing(batch_id)
        return row[0], json.loads(row[1]), json.loads(row[2])


class StoreError(Exception):
    """A fatal database error (exit status 2)."""

    def __init__(self, message: str, *, filename: str | None = None):
        super().__init__(message)
        self.message = message
        self.filename = filename


class BatchMissing(Exception):
    """The requested batch id is not present in the database."""

    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        super().__init__(f"batch {batch_id!r} not found")
