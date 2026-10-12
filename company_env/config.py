"""Runtime configuration, read lazily from environment variables.

Values are read on every call (not at import time) so tests can point the
environment at a temporary directory without re-importing modules.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.environ.get("COMPANY_ENV_DATA_DIR", ROOT / "data"))


def documents_dir() -> Path:
    return data_dir() / "documents"


def db_path() -> Path:
    return Path(os.environ.get("COMPANY_ENV_DB", data_dir() / "finance.db"))


def admin_enabled() -> bool:
    """Admin + fault-injection endpoints. OFF by default; the agent never needs them."""
    return os.environ.get("COMPANY_ENV_ADMIN", "0") == "1"


def enforce_unique_invoice() -> bool:
    """If on, POST /invoices rejects a repeated (vendor, invoice_number) with 409.

    Off by default on purpose: many real systems don't enforce this, so the
    *agent* must detect duplicates itself.
    """
    return os.environ.get("COMPANY_ENV_ENFORCE_UNIQUE", "0") == "1"
