"""Interactive Web UI and Demonstration API for the Autonomous AI Task Worker.

Connects the browser dashboard directly to:
- AgentOrchestrator (Categories A-E natural language execution, clarification & approval resumption)
- DocumentReading / Extraction & Verification pipeline (multi-format inspection, OCR & grounding checks)
- OperationEngine & Public Finance API (/invoices, /payments, read-after-write verification, idempotency)
- FaultRegistry & Request Log (HTTP 503 before-commit & HTTP 504 after-commit fault simulation)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import re
import shutil
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
import zipfile

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import openpyxl
from pydantic import BaseModel, Field, ValidationError

from agent.finance.finance_client import FinanceClient
from agent.finance.reliability import ReliabilityPolicy
from agent.orchestrator import AgentOrchestrator
from agents.extractor import (
    DEFAULT_REQUESTED_FIELDS,
    discover_files,
    extract_fields,
    read_document as read_doc_content,
)
from agents.models import (
    ExtractionResult,
    OrchestratorResponse,
    TaskStatus,
)
from agents.reliability import GovernanceViolationError
from agents.verification import verify_extraction_result, verify_read_after_write

from . import api as public_api
from . import config, db
from .deps import get_db, get_store
from .documents import DocumentStore
from .errors import ApiError
from .faults import FaultSpec, registry
from .models import InvoiceCreate, InvoiceUpdate, PaymentCreate

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ---------------------------------------------------------------------------
# In-Process Public API Dispatcher (Preserves Fault Injection + Request Log + Validation)
# ---------------------------------------------------------------------------
class _SimResponse:
    def __init__(self, status_code: int, payload: Any, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload, default=str)

    def json(self) -> Any:
        return self._payload


class DirectEnvClient:
    """Executes public API requests against the real handlers, Pydantic schemas,
    SQLite database, FaultRegistry (before_commit / after_commit), and request_log.
    """

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> _SimResponse:
        method = method.upper()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        query_str = urlencode(clean_params) if clean_params else ""
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}

        fault = registry.match(method, path)
        if fault is not None and fault.spec.phase == "before_commit":
            if fault.spec.delay_s:
                time.sleep(min(fault.spec.delay_s, 2.0))
            err_code = "gateway_timeout" if fault.spec.status_code == 504 else "injected_fault"
            resp = _SimResponse(
                fault.spec.status_code,
                {"error": err_code, "detail": fault.spec.detail},
            )
            db.log_request(method, path, query_str, resp.status_code, fault.fault_id, fault.spec.phase)
            return resp

        resp = self._dispatch_handler(method, path, clean_params, json, hdrs)

        if fault is not None:
            # after_commit: real handler already committed to SQLite; response is replaced by fault!
            if fault.spec.delay_s:
                time.sleep(min(fault.spec.delay_s, 2.0))
            err_code = "gateway_timeout" if fault.spec.status_code == 504 else "injected_fault"
            resp = _SimResponse(
                fault.spec.status_code,
                {"error": err_code, "detail": fault.spec.detail},
            )

        db.log_request(
            method,
            path,
            query_str,
            resp.status_code,
            fault.fault_id if fault else None,
            fault.spec.phase if fault else None,
        )
        return resp

    def _dispatch_handler(
        self,
        method: str,
        path: str,
        params: Dict[str, Any],
        json_body: Optional[Dict[str, Any]],
        headers: Dict[str, str],
    ) -> _SimResponse:
        conn = db.connect()
        store = DocumentStore(config.documents_dir())
        try:
            if method == "GET" and path == "/documents/search":
                data = public_api.search_documents(
                    q=params.get("q", ""),
                    folder=params.get("folder"),
                    limit=int(params.get("limit", 20)),
                    store=store,
                )
                return _SimResponse(200, data)

            m_doc = re.fullmatch(r"/documents/([A-Za-z0-9\-]+)", path)
            if method == "GET" and m_doc:
                data = public_api.read_document(m_doc.group(1), store=store)
                return _SimResponse(200, data)

            if method == "GET" and path == "/invoices":
                data = public_api.list_invoices(
                    vendor=params.get("vendor"),
                    invoice_number=params.get("invoice_number"),
                    status=params.get("status"),
                    q=params.get("q"),
                    conn=conn,
                )
                return _SimResponse(200, data)

            m_inv = re.fullmatch(r"/invoices/(INV-[A-Za-z0-9\-]+)", path)
            if method == "GET" and m_inv:
                data = public_api.get_invoice(m_inv.group(1), conn=conn)
                return _SimResponse(200, data)

            if method == "POST" and path == "/invoices":
                body = InvoiceCreate.model_validate(json_body or {})
                fastapi_resp = Response(status_code=201)
                idem_key = headers.get("idempotency-key")
                data = public_api.create_invoice(
                    body=body,
                    response=fastapi_resp,
                    idempotency_key=idem_key,
                    conn=conn,
                )
                return _SimResponse(fastapi_resp.status_code or 201, data, dict(fastapi_resp.headers))

            if method == "PATCH" and m_inv:
                body = InvoiceUpdate.model_validate(json_body or {})
                data = public_api.update_invoice(m_inv.group(1), body=body, conn=conn)
                return _SimResponse(200, data)

            if method == "GET" and path == "/payments":
                data = public_api.list_payments(record_id=params.get("record_id"), conn=conn)
                return _SimResponse(200, data)

            m_pay = re.fullmatch(r"/payments/(PAY-[A-Za-z0-9\-]+)", path)
            if method == "GET" and m_pay:
                data = public_api.get_payment(m_pay.group(1), conn=conn)
                return _SimResponse(200, data)

            if method == "POST" and path == "/payments":
                body = PaymentCreate.model_validate(json_body or {})
                data = public_api.create_payment(body=body, conn=conn)
                return _SimResponse(201, data)

            return _SimResponse(404, {"error": "not_found", "detail": f"Unknown endpoint {method} {path}"})
        except ApiError as exc:
            return _SimResponse(exc.status, {"error": exc.code, "detail": exc.detail})
        except ValidationError as exc:
            return _SimResponse(422, {"error": "validation_error", "detail": exc.errors()})
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# UI Orchestrator Session Manager
# ---------------------------------------------------------------------------
class _UISession:
    def __init__(self) -> None:
        self.direct_client = DirectEnvClient()
        self.finance_client = FinanceClient(client=self.direct_client)
        self.policy = ReliabilityPolicy(max_retries=4, base_delay_s=0.05, max_delay_s=0.2)
        self.orchestrator = AgentOrchestrator(
            finance_client=self.finance_client,
            policy=self.policy,
        )
        self.task_history: List[Dict[str, Any]] = []

    def record_task(self, action_label: str, prompt: str, resp: OrchestratorResponse) -> Dict[str, Any]:
        serialized = _serialize_orchestrator_response(resp)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action_label,
            "prompt": prompt,
            **serialized,
        }
        self.task_history.insert(0, entry)
        return entry


_SESSION = _UISession()


def _to_jsonable(val: Any) -> Any:
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, Path):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    return val


def _serialize_orchestrator_response(resp: OrchestratorResponse) -> Dict[str, Any]:
    return {
        "task_id": resp.task_id,
        "status": resp.status.value if hasattr(resp.status, "value") else str(resp.status),
        "success": resp.success,
        "summary": resp.summary,
        "answer": resp.answer,
        "evidence": resp.evidence,
        "records_created": resp.records_created,
        "payments_created": resp.payments_created,
        "failures": [
            {
                "category": f.category.value if hasattr(f.category, "value") else str(f.category),
                "message": f.message,
                "field_name": f.field_name,
                "file_path": Path(f.file_path).name if f.file_path else None,
                "details": f.details,
            }
            for f in (resp.failures or [])
        ],
        "audit_log": list(resp.audit_log or []),
        "clarification_prompt": resp.clarification_prompt,
        "clarification_options": list(resp.clarification_options or []),
        "approval_required": resp.approval_required,
        "approval_token": resp.approval_token,
        "approval_details": resp.approval_details,
        "execution_trace": getattr(resp, "execution_trace", None),
    }


def _serialize_extraction_result(res: ExtractionResult) -> Dict[str, Any]:
    fields_out: Dict[str, Any] = {}
    for k, f in res.fields.items():
        fields_out[k] = {
            "field_name": f.field_name,
            "extracted_value": _to_jsonable(f.extracted_value),
            "normalized_value": _to_jsonable(f.normalized_value),
            "source_document": Path(f.source_document).name if f.source_document else "",
            "source_location": f.source_location.to_string() if f.source_location else None,
            "source_evidence": f.source_evidence,
            "extraction_method": f.extraction_method,
            "verification_status": (
                f.verification_status.value
                if hasattr(f.verification_status, "value")
                else str(f.verification_status)
            ),
            "unresolved_issue": f.unresolved_issue,
            "confidence": f.confidence,
        }
    return {
        "document_path": res.document_path,
        "status": res.status,
        "requires_human_review": res.requires_human_review,
        "recovery_log": res.recovery_log,
        "fields": fields_out,
        "failures": [
            {
                "category": fl.category.value if hasattr(fl.category, "value") else str(fl.category),
                "message": fl.message,
                "field_name": fl.field_name,
            }
            for fl in res.failures
        ],
    }


# ---------------------------------------------------------------------------
# Request Models for UI Endpoints
# ---------------------------------------------------------------------------
class TaskRunBody(BaseModel):
    prompt: str = Field(min_length=1)


class TaskClarifyBody(BaseModel):
    task_id: str
    selected_option: str


class TaskApproveBody(BaseModel):
    task_id: str
    approval_token: str
    approved: bool = True
    approver_role: Optional[str] = None


class DocumentInspectBody(BaseModel):
    rel_path: str
    requested_fields: Optional[List[str]] = None


class SampleDocCreateBody(BaseModel):
    filename: str
    format: str = "txt"
    content: str = ""


class UIRecordInvoiceBody(BaseModel):
    vendor: str = Field(min_length=1)
    invoice_number: str = Field(min_length=1)
    amount: float = Field(gt=0)
    currency: str = "INR"
    due_date: str
    source_document: Optional[str] = None
    mode: str = "engine"  # "engine" (with duplicate check + 503/504 recovery + RAW verify) or "direct_api"
    idempotency_key: Optional[str] = None


class UIPaymentRequestBody(BaseModel):
    record_id: str


class UIPaymentBypassTestBody(BaseModel):
    record_id: str
    amount: float
    scenario: str = "missing_token"  # "missing_token", "forged_token", "wrong_role"


# ---------------------------------------------------------------------------
# Server-Rendered HTML Pages
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def index(request: Request, live: int = 0, conn=Depends(get_db)):
    invoices = [db.invoice_to_dict(r) for r in conn.execute("SELECT * FROM invoices ORDER BY id DESC")]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY id DESC")]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"invoices": invoices, "payments": payments, "live": live},
    )


@router.get("/ui/invoices/{record_id}", response_class=HTMLResponse)
def invoice_detail(
    request: Request,
    record_id: str,
    conn=Depends(get_db),
    store: DocumentStore = Depends(get_store),
):
    row = conn.execute("SELECT * FROM invoices WHERE record_id=?", (record_id,)).fetchone()
    if row is None:
        raise ApiError(404, "invoice_not_found", f"No invoice record {record_id}")
    inv = db.invoice_to_dict(row)
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE record_id=?", (record_id,))]
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "inv": inv,
            "payments": payments,
            "doc_id": store.id_for_filename(inv["source_document"]),
            "live": 0,
        },
    )


@router.get("/ui/activity", response_class=HTMLResponse)
def activity(request: Request, live: int = 0, conn=Depends(get_db)):
    rows = [dict(r) for r in conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT 200")]
    return templates.TemplateResponse(request, "activity.html", {"rows": rows, "live": live})


# ---------------------------------------------------------------------------
# Interactive UI JSON Endpoints (/ui/api/*)
# ---------------------------------------------------------------------------
@router.get("/ui/api/state")
def ui_state(conn=Depends(get_db), store: DocumentStore = Depends(get_store)):
    invoices = [db.invoice_to_dict(r) for r in conn.execute("SELECT * FROM invoices ORDER BY id DESC")]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY id DESC")]
    req_log = [dict(r) for r in conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT 100")]
    docs = _list_all_documents(store)
    return {
        "invoices": invoices,
        "payments": payments,
        "faults": registry.list(),
        "request_log": req_log,
        "task_history": _SESSION.task_history[:50],
        "documents": docs,
    }


@router.post("/ui/api/tasks/run")
def ui_run_task(body: TaskRunBody):
    resp = _SESSION.orchestrator.run(body.prompt)
    return _SESSION.record_task("run", body.prompt, resp)


@router.post("/ui/api/tasks/clarify")
def ui_clarify_task(body: TaskClarifyBody):
    resp = _SESSION.orchestrator.resume_with_clarification(body.task_id, body.selected_option)
    return _SESSION.record_task(
        f"clarify({body.selected_option})",
        f"Clarified {body.task_id} -> {body.selected_option}",
        resp,
    )


@router.post("/ui/api/tasks/approve")
def ui_approve_task(body: TaskApproveBody):
    resp = _SESSION.orchestrator.resume_with_approval(
        task_id=body.task_id,
        approval_token=body.approval_token,
        approved=body.approved,
        approver_role=body.approver_role,
    )
    decision = "approved" if body.approved else "denied"
    return _SESSION.record_task(
        f"approval({decision})",
        f"Approval {decision} for {body.task_id} (role={body.approver_role or 'auto'})",
        resp,
    )


def _list_all_documents(store: DocumentStore) -> List[Dict[str, Any]]:
    docs_root = config.documents_dir()
    files = discover_files(docs_root)
    items: List[Dict[str, Any]] = []
    for fpath in files:
        rel = fpath.relative_to(docs_root).as_posix()
        st = fpath.stat()
        folder = fpath.relative_to(docs_root).parts[0] if len(fpath.relative_to(docs_root).parts) > 1 else "root"
        doc_id = store.id_for_filename(fpath.name) if fpath.suffix.lower() == ".pdf" else None
        items.append(
            {
                "rel_path": rel,
                "filename": fpath.name,
                "folder": folder,
                "format": fpath.suffix.lower().lstrip("."),
                "size_bytes": st.st_size,
                "uploaded_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                "document_id": doc_id,
            }
        )
    return items


@router.get("/ui/api/documents")
def ui_list_documents(store: DocumentStore = Depends(get_store)):
    items = _list_all_documents(store)
    return {"count": len(items), "items": items}


@router.get("/ui/api/documents/download")
def ui_download_document(rel_path: str = Query(...)):
    docs_root = config.documents_dir().resolve()
    target = (docs_root / rel_path).resolve()
    try:
        target.relative_to(docs_root)
    except ValueError:
        raise ApiError(403, "forbidden", "Path traversal outside documents directory is not allowed")
    if not target.is_file():
        raise ApiError(404, "document_not_found", f"Document not found: {rel_path}")
    return FileResponse(target, filename=target.name)


@router.post("/ui/api/documents/inspect")
def ui_inspect_document(body: DocumentInspectBody):
    docs_root = config.documents_dir().resolve()
    target = (docs_root / body.rel_path).resolve()
    try:
        target.relative_to(docs_root)
    except ValueError:
        raise ApiError(403, "forbidden", "Path traversal outside documents directory is not allowed")
    if not target.is_file():
        raise ApiError(404, "document_not_found", f"File not found: {body.rel_path}")

    req_fields = body.requested_fields or (
        DEFAULT_REQUESTED_FIELDS + ["supersedes", "payment_terms", "expiry_date"]
    )
    try:
        doc = read_doc_content(target)
        ext = extract_fields(doc, requested_fields=req_fields)
        ext = verify_extraction_result(ext, doc_raw_text=doc.raw_text)
        return {
            "ok": True,
            "rel_path": body.rel_path,
            "filename": target.name,
            "file_format": doc.file_format,
            "raw_text": doc.raw_text,
            "metadata": doc.metadata,
            "pages": [
                {
                    "page_num": p.page_num,
                    "text": p.text,
                    "is_ocr": p.is_ocr,
                    "image_count": p.image_count,
                }
                for p in doc.pages
            ],
            "tables": doc.tables,
            "structured_data": doc.structured_data,
            "extraction": _serialize_extraction_result(ext),
        }
    except Exception as exc:
        return {
            "ok": False,
            "rel_path": body.rel_path,
            "filename": target.name,
            "error": str(exc),
        }


@router.post("/ui/api/documents/create-sample")
def ui_create_sample_document(body: SampleDocCreateBody):
    docs_root = config.documents_dir().resolve()
    custom_dir = docs_root / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^A-Za-z0-9._\-]", "_", Path(body.filename).stem) or "sample_invoice"
    fmt = body.format.lower().lstrip(".")
    if fmt not in ("txt", "json", "csv", "xlsx", "docx"):
        raise ApiError(422, "unsupported_format", f"Unsupported sample format: {fmt}")

    target = custom_dir / f"{safe_name}.{fmt}"
    content = body.content.strip()

    if fmt in ("txt", "json", "csv"):
        target.write_text(content, encoding="utf-8")
    elif fmt == "xlsx":
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Invoice"
        for line in content.splitlines():
            parts = [p.strip() for p in (line.split(",") if "," in line else line.split(":", 1))]
            ws.append(parts)
        wb.save(target)
    elif fmt == "docx":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                "</Types>",
            )
            p_xml = "".join(
                f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in content.splitlines()
            )
            z.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{p_xml}</w:body>"
                "</w:document>",
            )
        target.write_bytes(buf.getvalue())

    rel_path = target.relative_to(docs_root).as_posix()
    return {"status": "created", "rel_path": rel_path, "filename": target.name}


@router.post("/ui/api/invoices/record")
def ui_record_invoice(body: UIRecordInvoiceBody):
    if body.mode == "direct_api":
        headers = {"Idempotency-Key": body.idempotency_key} if body.idempotency_key else {}
        sim_resp = _SESSION.direct_client.request(
            "POST",
            "/invoices",
            json={
                "vendor": body.vendor,
                "invoice_number": body.invoice_number,
                "amount": body.amount,
                "currency": body.currency,
                "due_date": body.due_date,
                "source_document": body.source_document,
            },
            headers=headers,
        )
        if sim_resp.status_code >= 400:
            return {
                "ok": False,
                "mode": "direct_api",
                "status_code": sim_resp.status_code,
                "error": sim_resp.json(),
            }
        rec = sim_resp.json()
        persisted = _SESSION.direct_client.request("GET", f"/invoices/{rec['record_id']}").json()
        raw_ok, raw_err = verify_read_after_write(
            intended_fields={
                "invoice_number": body.invoice_number,
                "amount": body.amount,
                "due_date": body.due_date,
            },
            persisted_record=persisted,
        )
        return {
            "ok": raw_ok,
            "mode": "direct_api",
            "status_code": sim_resp.status_code,
            "idempotent_replay": sim_resp.headers.get("Idempotent-Replay") == "true",
            "record": persisted,
            "read_after_write_verified": raw_ok,
            "verification_error": raw_err,
        }

    # Reliable OperationEngine mode (pre-write duplicate check, 503 retry, 504 reconciliation, RAW verification)
    before_log_len = len(_SESSION.orchestrator.engine.audit_log)
    try:
        res = _SESSION.orchestrator.engine.record_invoice(
            vendor=body.vendor,
            invoice_number=body.invoice_number,
            amount=body.amount,
            due_date=body.due_date,
            currency=body.currency,
            source_document=body.source_document,
        )
        new_logs = _SESSION.orchestrator.engine.audit_log[before_log_len:]
        return {
            "ok": True,
            "mode": "engine",
            "operation_status": res["status"],
            "record": res["record"],
            "read_after_write_verified": res["verified"],
            "reconciled_after_timeout": res["reconciled_after_timeout"],
            "audit_log": new_logs,
        }
    except Exception as exc:
        new_logs = _SESSION.orchestrator.engine.audit_log[before_log_len:]
        return {
            "ok": False,
            "mode": "engine",
            "error": str(exc),
            "audit_log": new_logs,
        }


@router.patch("/ui/api/invoices/{record_id}")
def ui_patch_invoice(record_id: str, payload: Dict[str, Any]):
    sim_resp = _SESSION.direct_client.request("PATCH", f"/invoices/{record_id}", json=payload)
    if sim_resp.status_code >= 400:
        return {
            "ok": False,
            "status_code": sim_resp.status_code,
            "error": sim_resp.json(),
        }
    updated = sim_resp.json()
    persisted = _SESSION.direct_client.request("GET", f"/invoices/{record_id}").json()
    raw_ok, raw_err = verify_read_after_write(
        intended_fields={k: v for k, v in payload.items() if v is not None},
        persisted_record=persisted,
    )
    return {
        "ok": raw_ok,
        "status_code": sim_resp.status_code,
        "record": persisted,
        "read_after_write_verified": raw_ok,
        "verification_error": raw_err,
    }


@router.post("/ui/api/payments/request-approval")
def ui_request_payment_approval(body: UIPaymentRequestBody):
    prompt = f"Pay invoice {body.record_id}."
    resp = _SESSION.orchestrator.run(prompt)
    return _SESSION.record_task("payment_request", prompt, resp)


@router.post("/ui/api/payments/test-bypass")
def ui_test_payment_bypass(body: UIPaymentBypassTestBody):
    """Demonstrates that calling OperationEngine.execute_payment without a valid HMAC token
    or with an insufficient role is unconditionally blocked by PaymentApprovalGate.
    """
    token: Optional[str] = None
    role: Optional[str] = "supervisor"

    if body.scenario == "forged_token":
        token = "eyJyZWNvcmRfaWQiOiJJTlYtMTAwMiIsImFtb3VudCI6IjQ4MjAwLjAwIn0=.0000000000000000deadbeef"
    elif body.scenario == "wrong_role":
        token = _SESSION.orchestrator.approval_gate.issue_approval_request_token(
            task_id="demo-high-val",
            record_id=body.record_id,
            amount=max(body.amount, 50000.00),
            currency="INR",
        )
        role = "supervisor"  # Policy 3.b requires finance_manager for >= 50,000

    try:
        _SESSION.orchestrator.engine.execute_payment(
            record_id=body.record_id,
            amount=max(body.amount, 50000.00) if body.scenario == "wrong_role" else body.amount,
            approval_token=token or "",
            approver_role=role,
        )
        return {"blocked": False, "message": "Unexpectedly succeeded"}
    except GovernanceViolationError as exc:
        return {
            "blocked": True,
            "scenario": body.scenario,
            "exception_type": "GovernanceViolationError",
            "message": str(exc),
        }


@router.post("/ui/api/faults")
def ui_add_fault(spec: FaultSpec):
    state = registry.add(spec)
    return state.to_dict()


@router.delete("/ui/api/faults")
def ui_clear_faults():
    registry.clear()
    return {"status": "cleared", "items": []}


@router.post("/ui/api/reset")
def ui_reset_environment():
    conn = db.connect()
    try:
        db.reset_db(conn)
    finally:
        conn.close()
    registry.clear()
    _SESSION.task_history.clear()
    _SESSION.orchestrator.engine.audit_log.clear()
    custom_dir = config.documents_dir() / "custom"
    if custom_dir.exists():
        shutil.rmtree(custom_dir, ignore_errors=True)
    return {"status": "reset"}
