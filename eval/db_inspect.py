"""Independent verification helpers for the evaluation harness.

These read SQLite DIRECTLY, bypassing the HTTP API (and therefore bypassing
any injected faults). Use them to check what REALLY happened, never to give
the agent a shortcut.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from company_env import config


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, args=(), db_path=None) -> list[dict]:
    conn = _conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def invoices(db_path=None) -> list[dict]:
    return _rows("SELECT * FROM invoices ORDER BY id", db_path=db_path)


def invoices_matching(vendor: str, invoice_number: str, db_path=None) -> list[dict]:
    return _rows("SELECT * FROM invoices WHERE LOWER(vendor) LIKE LOWER(?) AND LOWER(invoice_number)=LOWER(?)",
                 (f"%{vendor}%", invoice_number), db_path)


def payments(db_path=None) -> list[dict]:
    return _rows("SELECT * FROM payments ORDER BY id", db_path=db_path)


def request_log(db_path=None) -> list[dict]:
    return _rows("SELECT * FROM request_log ORDER BY id", db_path=db_path)


def duplicate_groups(db_path=None) -> list[dict]:
    """(vendor, invoice_number) pairs stored more than once - your 'duplicate-record count'."""
    return _rows("SELECT LOWER(vendor) AS vendor, LOWER(invoice_number) AS invoice_number, COUNT(*) AS n "
                 "FROM invoices GROUP BY 1, 2 HAVING n > 1", db_path=db_path)


def snapshot(db_path=None) -> dict:
    """Whole-state snapshot; diff two snapshots to prove 'no side effects'."""
    return {"invoices": invoices(db_path), "payments": payments(db_path)}
