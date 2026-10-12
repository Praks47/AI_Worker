"""The finance application's public API: documents, invoices, payments.

This is the surface the agent's tools should call. Handlers always commit
before returning, so an injected `after_commit` fault leaves real state behind.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import FileResponse

from . import db
from . import config
from .deps import get_db, get_store
from .documents import DocumentStore
from .errors import ApiError
from .models import InvoiceCreate, InvoiceOut, InvoiceUpdate, PaymentCreate, PaymentOut
from agents.reliability import GovernanceViolationError
from agents.verification import PaymentApprovalGate

router = APIRouter()



# --------------------------------------------------------------- documents
@router.get("/documents/search")
def search_documents(q: str = "", folder: str | None = None,
                     limit: int = Query(20, ge=1, le=100),
                     store: DocumentStore = Depends(get_store)):
    """Keyword search over file names and document text (all terms must match)."""
    items = store.search(q, folder=folder, limit=limit)
    return {"count": len(items), "items": items}


@router.get("/documents/{document_id}")
def read_document(document_id: str, store: DocumentStore = Depends(get_store)):
    doc = store.get(document_id)
    if doc is None:
        raise ApiError(404, "document_not_found", f"No document with id {document_id}")
    return doc


@router.get("/documents/{document_id}/file")
def download_document(document_id: str, store: DocumentStore = Depends(get_store)):
    path = store.path_for(document_id)
    if path is None:
        raise ApiError(404, "document_not_found", f"No document with id {document_id}")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


# ---------------------------------------------------------------- invoices
def _invoice_or_404(conn, record_id: str) -> dict:
    row = conn.execute("SELECT * FROM invoices WHERE record_id=?", (record_id,)).fetchone()
    if row is None:
        raise ApiError(404, "invoice_not_found", f"No invoice record {record_id}")
    return db.invoice_to_dict(row)


@router.get("/invoices")
def list_invoices(vendor: str | None = None, invoice_number: str | None = None,
                  status: str | None = None, q: str | None = None,
                  conn=Depends(get_db)):
    """Filter records. vendor / invoice_number / q are case-insensitive substring matches."""
    sql, args = "SELECT * FROM invoices WHERE 1=1", []
    if vendor:
        sql += " AND vendor LIKE ?"; args.append(f"%{vendor}%")
    if invoice_number:
        sql += " AND invoice_number LIKE ?"; args.append(f"%{invoice_number}%")
    if status:
        sql += " AND LOWER(status)=LOWER(?)"; args.append(status)
    if q:
        sql += " AND (vendor LIKE ? OR invoice_number LIKE ? OR record_id LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY id DESC"
    items = [db.invoice_to_dict(r) for r in conn.execute(sql, args).fetchall()]
    return {"count": len(items), "items": items}


@router.get("/invoices/{record_id}", response_model=InvoiceOut)
def get_invoice(record_id: str, conn=Depends(get_db)):
    return _invoice_or_404(conn, record_id)


@router.post("/invoices", response_model=InvoiceOut, status_code=201)
def create_invoice(body: InvoiceCreate, response: Response,
                   idempotency_key: str | None = Header(default=None),
                   conn=Depends(get_db)):
    """Create an invoice record.

    Optional ``Idempotency-Key`` header: replaying the same key returns the
    originally created record (HTTP 200, ``Idempotent-Replay: true``) instead of
    creating another one.
    """
    if idempotency_key:
        row = conn.execute("SELECT record_id FROM idempotency_keys WHERE key=?",
                           (idempotency_key,)).fetchone()
        if row:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
            return _invoice_or_404(conn, row["record_id"])

    if config.enforce_unique_invoice():
        dup = conn.execute(
            "SELECT record_id FROM invoices WHERE LOWER(vendor)=LOWER(?) AND LOWER(invoice_number)=LOWER(?)",
            (body.vendor, body.invoice_number)).fetchone()
        if dup:
            raise ApiError(409, "duplicate_invoice",
                           f"Invoice {body.invoice_number} from {body.vendor} already exists as {dup['record_id']}")

    record_id = db.insert_invoice(
        conn, vendor=body.vendor, invoice_number=body.invoice_number, amount=body.amount,
        currency=body.currency, due_date=body.due_date.isoformat(),
        source_document=body.source_document, status="Pending")
    if idempotency_key:
        conn.execute("INSERT INTO idempotency_keys (key, record_id, created_at) VALUES (?,?,?)",
                     (idempotency_key, record_id, db.utcnow()))
    conn.commit()
    return _invoice_or_404(conn, record_id)


@router.patch("/invoices/{record_id}", response_model=InvoiceOut)
def update_invoice(record_id: str, body: InvoiceUpdate, conn=Depends(get_db)):
    current = _invoice_or_404(conn, record_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise ApiError(422, "no_fields", "Provide at least one field to update")
    if current["status"] == "Paid":
        raise ApiError(409, "invoice_locked", f"{record_id} is Paid and can no longer be edited")
    if "due_date" in changes and changes["due_date"] is not None:
        changes["due_date"] = changes["due_date"].isoformat()
    if "amount" in changes and changes["amount"] is not None:
        changes["amount"] = round(changes["amount"], 2)
    changes = {k: v for k, v in changes.items() if v is not None or k == "source_document"}
    sets = ", ".join(f"{k}=?" for k in changes)
    conn.execute(f"UPDATE invoices SET {sets}, updated_at=? WHERE record_id=?",
                 [*changes.values(), db.utcnow(), record_id])
    conn.commit()
    return _invoice_or_404(conn, record_id)


# ---------------------------------------------------------------- payments
@router.post("/payments", response_model=PaymentOut, status_code=201)
def create_payment(body: PaymentCreate,
                   x_approval_token: str | None = Header(default=None, alias="X-Approval-Token"),
                   x_approver_role: str | None = Header(default=None, alias="X-Approver-Role"),
                   conn=Depends(get_db)):
    """Submit a payment. Gated by cryptographic HMAC approval verification."""
    token = x_approval_token or body.approval_token
    if not token:
        raise ApiError(403, "forbidden", "Payment execution blocked: missing HMAC approval token")

    role = x_approver_role or body.approver_role

    inv = _invoice_or_404(conn, body.record_id)
    if inv["status"] == "Paid":
        raise ApiError(409, "already_paid", f"{body.record_id} has already been paid")
    if inv["status"] in ("Rejected", "On Hold"):
        raise ApiError(409, "invoice_not_payable", f"{body.record_id} is {inv['status']}")
    if round(body.amount, 2) != inv["amount"]:
        raise ApiError(422, "amount_mismatch",
                       f"Payment amount {body.amount} does not match invoice amount {inv['amount']}")

    # Cryptographic verification of approval token bound to this transaction
    gate = PaymentApprovalGate()
    try:
        payload = gate.verify_token(
            token=token,
            record_id=body.record_id,
            amount=body.amount,
            currency=inv["currency"],
            expected_scope="payment_submit",
            approver_role=role,
        )
    except GovernanceViolationError as exc:
        raise ApiError(403, "forbidden", f"Payment execution blocked by governance gate: {exc}")

    # Replay protection at database boundary
    nonce = payload.get("nonce")
    if not nonce:
        raise ApiError(403, "forbidden", "Payment execution blocked: malformed token nonce")

    row = conn.execute("SELECT nonce FROM consumed_approval_tokens WHERE nonce=?", (nonce,)).fetchone()
    if row:
        raise ApiError(403, "forbidden", "Payment execution blocked: approval token already consumed (replay detected)")

    payment_id = db.insert_payment(conn, record_id=body.record_id, amount=body.amount,
                                   currency=inv["currency"])
    conn.execute("UPDATE invoices SET status='Paid', updated_at=? WHERE record_id=?",
                 (db.utcnow(), body.record_id))
    conn.execute(
        "INSERT INTO consumed_approval_tokens (nonce, record_id, amount, consumed_at) VALUES (?,?,?,?)",
        (nonce, body.record_id, body.amount, db.utcnow()),
    )
    conn.commit()
    return dict(conn.execute("SELECT payment_id, record_id, amount, currency, status, created_at "
                             "FROM payments WHERE payment_id=?", (payment_id,)).fetchone())



@router.get("/payments")
def list_payments(record_id: str | None = None, conn=Depends(get_db)):
    sql, args = "SELECT payment_id, record_id, amount, currency, status, created_at FROM payments", []
    if record_id:
        sql += " WHERE record_id=?"; args.append(record_id)
    items = [dict(r) for r in conn.execute(sql + " ORDER BY id DESC", args).fetchall()]
    return {"count": len(items), "items": items}


@router.get("/payments/{payment_id}", response_model=PaymentOut)
def get_payment(payment_id: str, conn=Depends(get_db)):
    row = conn.execute("SELECT payment_id, record_id, amount, currency, status, created_at "
                       "FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
    if row is None:
        raise ApiError(404, "payment_not_found", f"No payment {payment_id}")
    return dict(row)
