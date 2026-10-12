from __future__ import annotations

from . import db
from . import config
from .documents import DocumentStore


def get_db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_store() -> DocumentStore:
    return DocumentStore(config.documents_dir())
