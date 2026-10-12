"""Autonomous AI Task Worker - Central Orchestrator across Categories A-E."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import uuid

from agent.finance.finance_client import FinanceClient
from agent.finance.operation_engine import FinanceTools, OperationEngine
from agent.finance.reliability import ReliabilityPolicy
from agents.extractor import (
    discover_files,
    extract_fields,
    read_document,
)
from agents.models import (
    ExtractionResult,
    FailureCategory,
    OrchestratorResponse,
    TaskStatus,
    VerificationStatus,
)
from agents.verification import PaymentApprovalGate, verify_extraction_result
from company_env import config as env_config


class AgentOrchestrator:
    """End-to-end orchestrator integrating Document Reading (Category A),
    Record Management (Category B), Cross-Document Workflows (Category C),
    Fault Handling & Investigation (Category D), and Governed Payments (Category E).
    """

    def __init__(
        self,
        finance_client: Optional[FinanceClient] = None,
        documents_dir: Optional[Path] = None,
        policy: Optional[ReliabilityPolicy] = None,
    ):
        self.client = finance_client or FinanceClient()
        self.policy = policy or ReliabilityPolicy()
        self.approval_gate = PaymentApprovalGate()
        self.engine = OperationEngine(
            finance_client=self.client,
            policy=self.policy,
            approval_gate=self.approval_gate,
        )
        self.finance_tools = FinanceTools(engine=self.engine)
        self._explicit_docs_dir = documents_dir
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def _get_documents_dir(self) -> Path:
        if self._explicit_docs_dir is not None:
            return Path(self._explicit_docs_dir)
        return env_config.documents_dir()

    # ------------------------------------------------------------------
    # Public Entry Points
    # ------------------------------------------------------------------
    def run(self, request: str) -> OrchestratorResponse:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        req_lower = request.lower().strip()

        # 1. Category E: Payment / Governed Action
        if re.search(r"\b(pay|settle|remit)\b", req_lower):
            return self._handle_payment_request(task_id, request)

        # 2. Category A: Contract expiry or non-invoice information retrieval
        if "contract" in req_lower or "expire" in req_lower or "msa" in req_lower or "policy" in req_lower:
            if "record" not in req_lower:
                return self._handle_contract_query(task_id, request)

        # 3. Categories A, B, C, D: Invoice extraction, verification, and recording
        return self._handle_invoice_workflow(task_id, request)

    def resume_with_clarification(
        self, task_id: str, selected_option: str
    ) -> OrchestratorResponse:
        """Resume a halted NEEDS_CLARIFICATION task with the user's selected candidate."""
        state = self._tasks.get(task_id)
        if not state or state.get("status") != TaskStatus.NEEDS_CLARIFICATION:
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.FAILED,
                success=False,
                summary=f"Task {task_id} is not awaiting clarification.",
            )

        candidates: List[ExtractionResult] = state.get("candidates", [])
        sel_clean = selected_option.strip().lower()

        chosen: Optional[ExtractionResult] = None
        for cand in candidates:
            fname = Path(cand.document_path or "").name.lower()
            stem = Path(cand.document_path or "").stem.lower()
            inv_num_f = cand.fields.get("invoice_number")
            inv_num = (
                str(inv_num_f.normalized_value or inv_num_f.extracted_value or "").lower()
                if inv_num_f
                else ""
            )
            if sel_clean in (fname, stem, inv_num) or sel_clean in fname:
                chosen = cand
                break

        if chosen is None:
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.FAILED,
                success=False,
                summary=f"Selected option '{selected_option}' did not match any candidate.",
            )

        return self._finalize_invoice_recording(
            task_id=task_id,
            extraction=chosen,
            request=state.get("request", ""),
            allow_ambiguous_date_resolution=True,
        )

    def resume_with_approval(
        self,
        task_id: str,
        approval_token: str,
        approved: bool,
        approver_role: Optional[str] = None,
    ) -> OrchestratorResponse:
        """Resume a halted NEEDS_APPROVAL payment task with supervisor decision."""
        state = self._tasks.get(task_id)
        if not state or state.get("status") != TaskStatus.NEEDS_APPROVAL:
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.FAILED,
                success=False,
                summary=f"Task {task_id} is not awaiting approval.",
            )

        record_id = state["record_id"]
        amount = state["amount"]
        currency = state.get("currency", "INR")
        vendor = state.get("vendor", "")
        invoice_number = state.get("invoice_number", "")

        if not approved:
            state["status"] = TaskStatus.COMPLETED
            msg = (
                f"Payment for {record_id} ({vendor} {invoice_number}, {currency} {amount:,.2f}) "
                f"was denied by supervisor. No payment executed."
            )
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                success=True,
                summary=msg,
                answer=msg,
            )

        effective_role = approver_role or PaymentApprovalGate.required_role(amount)
        try:
            res = self.engine.execute_payment(
                record_id=record_id,
                amount=float(amount),
                approval_token=approval_token,
                currency=currency,
                approver_role=effective_role,
            )
            state["status"] = TaskStatus.COMPLETED
            payment = res.get("payment", {})
            pay_id = payment.get("payment_id", "")
            msg = (
                f"Approved and paid invoice {record_id} ({vendor} {invoice_number}) "
                f"for {currency} {float(amount):,.2f} (Payment ID: {pay_id}). Verified status=Paid."
            )
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                success=True,
                summary=msg,
                answer=msg,
                payments_created=[payment] if payment else [],
                audit_log=list(self.engine.audit_log),
            )
        except Exception as exc:
            state["status"] = TaskStatus.FAILED
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.FAILED,
                success=False,
                summary=f"Payment execution failed: {exc}",
                answer=str(exc),
            )

    # ------------------------------------------------------------------
    # Category A: Contract & Policy Information Retrieval
    # ------------------------------------------------------------------
    def _handle_contract_query(self, task_id: str, request: str) -> OrchestratorResponse:
        docs_dir = self._get_documents_dir()
        contracts_dir = docs_dir / "contracts"
        search_dir = contracts_dir if contracts_dir.exists() else docs_dir

        vendor = self._extract_vendor_from_query(request)
        files = discover_files(search_dir)

        for fpath in files:
            try:
                doc = read_document(fpath)
            except Exception:
                continue
            if vendor and not self._content_matches_vendor(doc.raw_text, fpath.name, vendor):
                continue

            ext = extract_fields(doc, requested_fields=["vendor", "expiry_date", "payment_terms"])
            ext = verify_extraction_result(ext, doc_raw_text=doc.raw_text)
            exp_field = ext.fields.get("expiry_date")
            if exp_field and (exp_field.normalized_value or exp_field.extracted_value):
                exp_val = str(exp_field.normalized_value or exp_field.extracted_value)
                doc_name = fpath.name
                ev = {
                    "field": "expiry_date",
                    "value": exp_val,
                    "source_file": doc_name,
                    "page": exp_field.source_location.page if exp_field.source_location else 1,
                    "line": exp_field.source_location.line_number if exp_field.source_location else None,
                    "snippet": exp_field.source_evidence,
                }
                answer = (
                    f"The contract with {vendor or 'the vendor'} ({doc_name}) expires on {exp_val}. "
                    f"Evidence: \"{exp_field.source_evidence}\" (page {ev['page']})."
                )
                return OrchestratorResponse(
                    task_id=task_id,
                    status=TaskStatus.COMPLETED,
                    success=True,
                    summary=answer,
                    answer=answer,
                    evidence=ev,
                )

        return OrchestratorResponse(
            task_id=task_id,
            status=TaskStatus.COMPLETED,
            success=False,
            summary="Could not find contract expiry date in available documents.",
            answer="No verified contract expiry date found.",
        )

    # ------------------------------------------------------------------
    # Categories A, B, C, D: Invoice Extraction & Recording Workflow
    # ------------------------------------------------------------------
    def _handle_invoice_workflow(self, task_id: str, request: str) -> OrchestratorResponse:
        docs_dir = self._get_documents_dir()
        invoices_dir = docs_dir / "invoices"
        search_dir = invoices_dir if invoices_dir.exists() else docs_dir

        vendor = self._extract_vendor_from_query(request)
        all_files = discover_files(search_dir)

        req_fields = [
            "vendor",
            "invoice_number",
            "invoice_date",
            "due_date",
            "subtotal",
            "tax",
            "total_amount",
            "currency",
            "supersedes",
            "payment_terms",
        ]

        extracted_candidates: List[ExtractionResult] = []
        matching_paths: List[str] = []

        for fpath in all_files:
            try:
                doc = read_document(fpath)
            except Exception:
                continue
            if vendor and not self._content_matches_vendor(doc.raw_text, fpath.name, vendor):
                continue
            matching_paths.append(str(fpath))
            ext = extract_fields(doc, requested_fields=req_fields)
            ext = verify_extraction_result(ext, doc_raw_text=doc.raw_text)
            extracted_candidates.append(ext)

        # D2: Missing document -> report honestly, never invent data
        if not extracted_candidates:
            msg = f"No invoice found for {vendor or 'requested vendor'}; nothing invented or recorded."
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                success=True,
                summary=msg,
                answer=msg,
            )

        # C1: Filter out superseded invoices
        superseded_numbers = set()
        for ext in extracted_candidates:
            sup_f = ext.fields.get("supersedes")
            if sup_f and (sup_f.normalized_value or sup_f.extracted_value):
                superseded_numbers.add(
                    str(sup_f.normalized_value or sup_f.extracted_value).strip().upper()
                )

        active_candidates: List[ExtractionResult] = []
        for ext in extracted_candidates:
            inv_f = ext.fields.get("invoice_number")
            inv_num = (
                str(inv_f.normalized_value or inv_f.extracted_value or "").strip().upper()
                if inv_f
                else ""
            )
            if inv_num and inv_num in superseded_numbers:
                continue
            active_candidates.append(ext)

        if not active_candidates:
            active_candidates = extracted_candidates

        # D4: Multiple candidates with ambiguous dates or unresolvable ordering -> ask for clarification
        if len(active_candidates) > 1:
            has_ambiguous_date = any(
                any(
                    f.category == FailureCategory.AMBIGUOUS_OR_CONFLICTING_INFORMATION
                    for f in ext.failures
                )
                or (
                    ext.fields.get("invoice_date") is not None
                    and ext.fields["invoice_date"].verification_status
                    == VerificationStatus.NEEDS_HUMAN_REVIEW
                )
                for ext in active_candidates
            )

            dated_candidates = []
            for ext in active_candidates:
                idf = ext.fields.get("invoice_date")
                dval = idf.normalized_value if idf else None
                if isinstance(dval, date):
                    dated_candidates.append((dval.isoformat(), ext))
                elif isinstance(dval, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", dval):
                    dated_candidates.append((dval, ext))

            if has_ambiguous_date or len(dated_candidates) < len(active_candidates):
                options = [Path(ext.document_path or "").name for ext in active_candidates]
                prompt = (
                    f"Found {len(active_candidates)} candidate invoices for {vendor} with ambiguous dates: "
                    f"{', '.join(options)}. Please clarify which invoice should be recorded."
                )
                self._tasks[task_id] = {
                    "status": TaskStatus.NEEDS_CLARIFICATION,
                    "candidates": active_candidates,
                    "request": request,
                }
                return OrchestratorResponse(
                    task_id=task_id,
                    status=TaskStatus.NEEDS_CLARIFICATION,
                    success=False,
                    summary=prompt,
                    answer=prompt,
                    clarification_prompt=prompt,
                    clarification_options=options,
                )

            dated_candidates.sort(key=lambda item: item[0], reverse=True)
            chosen = dated_candidates[0][1]
        else:
            chosen = active_candidates[0]

        return self._finalize_invoice_recording(
            task_id=task_id,
            extraction=chosen,
            request=request,
            allow_ambiguous_date_resolution=False,
        )

    def _finalize_invoice_recording(
        self,
        task_id: str,
        extraction: ExtractionResult,
        request: str,
        allow_ambiguous_date_resolution: bool = False,
    ) -> OrchestratorResponse:
        doc_name = Path(extraction.document_path or "").name

        # D3: Check for corrupted/unreadable total amount
        total_f = extraction.fields.get("total_amount") or extraction.fields.get("total")
        has_corrupted_amount = (
            total_f is None
            or total_f.normalized_value is None
            or total_f.verification_status in (VerificationStatus.FAILED, VerificationStatus.UNVERIFIED)
        )
        if has_corrupted_amount:
            raw_amt = total_f.extracted_value if total_f else "missing"
            msg = (
                f"Escalated for human review: invoice amount in {doc_name} is unreadable/corrupted "
                f"(extracted raw value: '{raw_amt}'). Refusing to guess or write unverified data."
            )
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.ESCALATED,
                success=False,
                summary=msg,
                answer=msg,
                failures=extraction.failures,
            )

        vendor_f = extraction.fields.get("vendor")
        inv_num_f = extraction.fields.get("invoice_number")
        curr_f = extraction.fields.get("currency")
        due_f = extraction.fields.get("due_date")
        inv_date_f = extraction.fields.get("invoice_date")

        vendor_val = str(
            (vendor_f.normalized_value or vendor_f.extracted_value)
            if (vendor_f and (vendor_f.normalized_value or vendor_f.extracted_value))
            else self._extract_vendor_from_query(request) or "Unknown Vendor"
        )
        inv_num_val = str(
            (inv_num_f.normalized_value or inv_num_f.extracted_value) if inv_num_f else ""
        )
        amount_val = float(total_f.normalized_value)
        currency_val = str(
            (curr_f.normalized_value or curr_f.extracted_value or "INR")
            if (curr_f and (curr_f.normalized_value or curr_f.extracted_value))
            else "INR"
        )

        due_date_val: Optional[str] = None
        if due_f and due_f.normalized_value:
            due_date_val = (
                due_f.normalized_value.isoformat()
                if isinstance(due_f.normalized_value, date)
                else str(due_f.normalized_value)
            )
        elif due_f and due_f.extracted_value and allow_ambiguous_date_resolution:
            m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", str(due_f.extracted_value).strip())
            if m:
                d_part, m_part, y_part = int(m.group(1)), int(m.group(2)), int(m.group(3))
                due_date_val = date(y_part, m_part, d_part).isoformat()

        # D5: Missing due date -> check MSA contract for payment terms derivation
        derivation_note = ""
        if not due_date_val:
            inv_d_str: Optional[str] = None
            if inv_date_f and inv_date_f.normalized_value:
                inv_d_str = (
                    inv_date_f.normalized_value.isoformat()
                    if isinstance(inv_date_f.normalized_value, date)
                    else str(inv_date_f.normalized_value)
                )
            derived_due, note = self._derive_due_date_from_contract(
                vendor=vendor_val,
                invoice_date_str=inv_d_str,
            )
            if derived_due:
                due_date_val = derived_due
                derivation_note = note

        if not due_date_val:
            msg = f"Escalated: due_date is missing for invoice {inv_num_val} and could not be derived from contract."
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.ESCALATED,
                success=False,
                summary=msg,
                answer=msg,
            )

        # Execute reliable record creation (includes D1 duplicate check, D7 503 retry, D6 504 reconciliation, and read-after-write verification)
        op_result = self.engine.record_invoice(
            vendor=vendor_val,
            invoice_number=inv_num_val,
            amount=amount_val,
            due_date=due_date_val,
            currency=currency_val,
            source_document=doc_name,
        )

        record = op_result["record"]
        rec_id = record.get("record_id", "")

        if op_result["status"] == "duplicate":
            msg = (
                f"Invoice {inv_num_val} from {vendor_val} already exists as {rec_id} "
                f"(amount: {currency_val} {amount_val:,.2f}, due: {record.get('due_date')}). "
                f"Skipped duplicate creation."
            )
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                success=True,
                summary=msg,
                answer=msg,
                records_created=[],
                audit_log=list(self.engine.audit_log),
            )

        msg = (
            f"Recorded invoice {inv_num_val} ({rec_id}) from {vendor_val}: "
            f"amount {currency_val} {amount_val:,.2f}, due {due_date_val} (source: {doc_name})."
        )
        if derivation_note:
            msg = f"{msg} {derivation_note}"

        ev = {
            "source_document": doc_name,
            "invoice_number": inv_num_val,
            "amount": amount_val,
            "due_date": due_date_val,
            "snippet": total_f.source_evidence,
        }

        return OrchestratorResponse(
            task_id=task_id,
            status=TaskStatus.COMPLETED,
            success=True,
            summary=msg,
            answer=msg,
            evidence=ev,
            records_created=[record],
            audit_log=list(self.engine.audit_log),
        )

    def _derive_due_date_from_contract(
        self, vendor: str, invoice_date_str: Optional[str]
    ) -> tuple[Optional[str], str]:
        if not invoice_date_str:
            return None, ""
        try:
            inv_date = date.fromisoformat(invoice_date_str)
        except ValueError:
            return None, ""

        docs_dir = self._get_documents_dir()
        contracts_dir = docs_dir / "contracts"
        if not contracts_dir.exists():
            return None, ""

        vendor_key = vendor.split()[0].lower()

        for fpath in discover_files(contracts_dir):
            try:
                doc = read_document(fpath)
            except Exception:
                continue
            if vendor_key not in doc.raw_text.lower() and vendor_key not in fpath.name.lower():
                continue
            m = re.search(r"Net\s+(\d+)\s*days?", doc.raw_text, re.IGNORECASE)
            if m:
                net_days = int(m.group(1))
                derived = (inv_date + timedelta(days=net_days)).isoformat()
                note = (
                    f"Derived due date {derived} from Master Services Agreement ({fpath.name}: "
                    f"Net {net_days} days from invoice date {invoice_date_str})."
                )
                return derived, note

        return None, ""

    # ------------------------------------------------------------------
    # Category E: Governed Payment Execution
    # ------------------------------------------------------------------
    def _handle_payment_request(self, task_id: str, request: str) -> OrchestratorResponse:
        m_id = re.search(r"\b(INV-\d+)\b", request, re.IGNORECASE)
        target_invoice: Optional[Dict[str, Any]] = None

        if m_id:
            rec_id = m_id.group(1).upper()
            try:
                resp = self.client.get_invoice(rec_id)
                target_invoice = resp.get("invoice", resp) if isinstance(resp, dict) else None
            except Exception:
                target_invoice = None

        if target_invoice is None:
            vendor = self._extract_vendor_from_query(request)
            if vendor:
                invs = self.client.list_invoices(vendor=vendor.split()[0])
                if invs:
                    target_invoice = invs[0]

        if target_invoice is None:
            msg = "Could not find matching invoice record to pay."
            return OrchestratorResponse(
                task_id=task_id,
                status=TaskStatus.FAILED,
                success=False,
                summary=msg,
                answer=msg,
            )

        record_id = target_invoice["record_id"]
        amount = float(target_invoice["amount"])
        currency = target_invoice.get("currency", "INR")
        vendor = target_invoice.get("vendor", "")
        invoice_number = target_invoice.get("invoice_number", "")

        approver_role = PaymentApprovalGate.required_role(amount)
        token = self.approval_gate.issue_approval_request_token(
            task_id=task_id,
            record_id=record_id,
            amount=amount,
            currency=currency,
            scope="payment_submit",
        )

        self._tasks[task_id] = {
            "status": TaskStatus.NEEDS_APPROVAL,
            "record_id": record_id,
            "amount": amount,
            "currency": currency,
            "vendor": vendor,
            "invoice_number": invoice_number,
            "approval_token": token,
        }

        msg = (
            f"Approval required before paying {record_id} ({vendor}, invoice {invoice_number}) "
            f"for {currency} {amount:,.2f}. Required role: {approver_role}."
        )
        return OrchestratorResponse(
            task_id=task_id,
            status=TaskStatus.NEEDS_APPROVAL,
            success=False,
            summary=msg,
            answer=msg,
            approval_required=True,
            approval_token=token,
            approval_details={
                "record_id": record_id,
                "amount": amount,
                "currency": currency,
                "required_role": approver_role,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_vendor_from_query(query: str) -> Optional[str]:
        known_vendors = [
            "Wayne Enterprises",
            "Pied Piper Supplies",
            "Umbrella Chemicals",
            "Acme",
            "Globex",
            "Hooli",
            "Initech",
            "Stark",
            "Soylent",
        ]
        q_lower = query.lower()
        for v in known_vendors:
            if v.lower() in q_lower:
                return v

        m = re.search(
            r"(?:from|with|for)\s+([A-Z][A-Za-z0-9\s&.\-]+?)(?:\s+and\b|\s+invoice\b|\s+expire|\?|\.|$)",
            query,
        )
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def _content_matches_vendor(text: str, filename: str, vendor: str) -> bool:
        vendor_tokens = [t.lower() for t in vendor.split() if len(t) > 2]
        if not vendor_tokens:
            vendor_tokens = [vendor.lower()]
        first_tok = vendor_tokens[0]
        if first_tok in filename.lower():
            return True
        for line in text.splitlines():
            if re.search(r"\b(bill\s*to|buyer|customer|client|ship\s*to)\b", line, re.IGNORECASE):
                continue
            if first_tok in line.lower():
                return True
        return False
