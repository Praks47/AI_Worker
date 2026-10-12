"""Test-harness controls. Mounted ONLY when COMPANY_ENV_ADMIN=1.

The agent must never be given these endpoints: they exist so an evaluation
harness can reset state and inject failures.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from . import db
from .faults import FaultSpec, registry

router = APIRouter(prefix="/_admin", tags=["admin (test harness only)"])


@router.post("/reset")
def reset():
    """Reset DB to the seed state and clear all injected faults."""
    conn = db.connect()
    try:
        db.reset_db(conn)
    finally:
        conn.close()
    registry.clear()
    return {"status": "reset"}


@router.get("/faults")
def list_faults():
    return {"items": registry.list()}


@router.post("/faults", status_code=201)
def add_fault(spec: FaultSpec):
    return registry.add(spec).to_dict()


@router.delete("/faults")
def clear_faults():
    registry.clear()
    return {"status": "cleared"}


@router.get("/request-log")
def request_log(limit: int = Query(200, ge=1, le=1000)):
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in reversed(rows)]}
