"""SQLite persistence for the simulated finance application."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       TEXT UNIQUE,
    vendor          TEXT NOT NULL,
    invoice_number  TEXT NOT NULL,
    amount          REAL NOT NULL CHECK (amount > 0),
    currency        TEXT NOT NULL,
    due_date        TEXT NOT NULL,
    source_document TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id  TEXT UNIQUE,
    record_id   TEXT NOT NULL REFERENCES invoices(record_id),
    amount      REAL NOT NULL,
    currency    TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    record_id  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    query       TEXT,
    status_code INTEGER NOT NULL,
    fault_id    TEXT,
    fault_phase TEXT
);
"""

# Records that already exist in the finance system before any task runs.
# (vendor, invoice_number, amount, currency, due_date, source_document, status)
SEED_INVOICES = [
    ("Acme Industrial Supplies", "AC-2026-061", 9800.00, "INR", "2026-07-15",
     "acme_invoice_2026_06.pdf", "Paid"),
    ("Globex Logistics", "GX-7731", 48200.00, "INR", "2026-09-20",
     "globex_invoice_2026_08.pdf", "Pending"),   # <- duplicate trap
    ("Stark Components", "SI-2026-114", 31500.00, "INR", "2026-10-10",
     "stark_invoice_2026_09.pdf", "Approved"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def insert_invoice(conn: sqlite3.Connection, *, vendor: str, invoice_number: str,
                   amount: float, currency: str, due_date: str,
                   source_document: str | None, status: str) -> str:
    """Insert an invoice and return its record_id. Caller commits."""
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO invoices (vendor, invoice_number, amount, currency, due_date,"
        " source_document, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (vendor, invoice_number, round(amount, 2), currency, due_date,
         source_document, status, now, now),
    )
    record_id = f"INV-{1000 + cur.lastrowid}"
    conn.execute("UPDATE invoices SET record_id=? WHERE id=?", (record_id, cur.lastrowid))
    return record_id


def insert_payment(conn: sqlite3.Connection, *, record_id: str, amount: float,
                   currency: str, status: str = "Submitted") -> str:
    cur = conn.execute(
        "INSERT INTO payments (record_id, amount, currency, status, created_at)"
        " VALUES (?,?,?,?,?)",
        (record_id, round(amount, 2), currency, status, utcnow()),
    )
    payment_id = f"PAY-{5000 + cur.lastrowid}"
    conn.execute("UPDATE payments SET payment_id=? WHERE id=?", (payment_id, cur.lastrowid))
    return payment_id


def seed(conn: sqlite3.Connection) -> None:
    for vendor, number, amount, currency, due, source, status in SEED_INVOICES:
        rid = insert_invoice(conn, vendor=vendor, invoice_number=number, amount=amount,
                             currency=currency, due_date=due, source_document=source,
                             status=status)
        if status == "Paid":
            insert_payment(conn, record_id=rid, amount=amount, currency=currency,
                           status="Completed")
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    """Drop everything and re-seed. Used by setup script, tests and the admin API."""
    conn.executescript(
        "DROP TABLE IF EXISTS payments; DROP TABLE IF EXISTS idempotency_keys;"
        "DROP TABLE IF EXISTS request_log; DROP TABLE IF EXISTS invoices;"
    )
    init_db(conn)
    seed(conn)


def ensure_db() -> None:
    """Create + seed the database on first start; leave existing data alone."""
    conn = connect()
    try:
        init_db(conn)
        if conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0:
            seed(conn)
    finally:
        conn.close()


def log_request(method: str, path: str, query: str, status_code: int,
                fault_id: str | None, fault_phase: str | None) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO request_log (ts, method, path, query, status_code, fault_id, fault_phase)"
            " VALUES (?,?,?,?,?,?,?)",
            (utcnow(), method, path, query or None, status_code, fault_id, fault_phase),
        )
        conn.commit()
    finally:
        conn.close()


def invoice_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("id", None)
    d["amount"] = round(d["amount"], 2)
    return d
