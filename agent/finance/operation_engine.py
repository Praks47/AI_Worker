"""Reliable Finance Operation Engine with Idempotency, 503/504 Recovery, Read-After-Write Verification, and Governance Gate."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.finance.finance_client import FinanceClient
from agents.reliability import (
    GovernanceViolationError,
    PostCommitTimeoutError,
    ReliabilityPolicy,
    TransientInfrastructureError,
    execute_with_retry,
    generate_idempotency_key,
)
from agents.verification import PaymentApprovalGate, verify_read_after_write


class OperationEngine:
    """Executes state-mutating finance operations with centralized reliability and verification."""

    def __init__(
        self,
        finance_client: FinanceClient,
        policy: Optional[ReliabilityPolicy] = None,
        approval_gate: Optional[PaymentApprovalGate] = None,
    ):
        self.client = finance_client
        self.policy = policy or ReliabilityPolicy()
        self.approval_gate = approval_gate or PaymentApprovalGate()
        self.audit_log: List[str] = []

    def find_existing_invoice(
        self, vendor: str, invoice_number: str
    ) -> Optional[Dict[str, Any]]:
        """Query existing invoices to prevent duplicates before writing or after 504 timeout."""
        candidates = execute_with_retry(
            lambda: self.client.list_invoices(invoice_number=invoice_number),
            policy=self.policy,
            operation_name=f"list_invoices({invoice_number})",
            audit_log=self.audit_log,
        )
        if candidates:
            for inv in candidates:
                if str(inv.get("invoice_number", "")).strip().upper() == invoice_number.strip().upper():
                    return inv

        vendor_prefix = vendor.split()[0] if vendor else ""
        if vendor_prefix:
            by_vendor = execute_with_retry(
                lambda: self.client.list_invoices(vendor=vendor_prefix),
                policy=self.policy,
                operation_name=f"list_invoices(vendor={vendor_prefix})",
                audit_log=self.audit_log,
            )
            if by_vendor:
                for inv in by_vendor:
                    if str(inv.get("invoice_number", "")).strip().upper() == invoice_number.strip().upper():
                        return inv
        return None

    def record_invoice(
        self,
        vendor: str,
        invoice_number: str,
        amount: float,
        due_date: str,
        currency: str = "INR",
        source_document: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an invoice with pre-write duplicate check, 503 retry, 504 reconciliation, and read-after-write verification."""
        # 1. Pre-write duplicate check
        existing = self.find_existing_invoice(vendor, invoice_number)
        if existing is not None:
            self.audit_log.append(
                f"record_invoice({invoice_number}): duplicate prevented (existing record_id={existing.get('record_id')})"
            )
            return {
                "status": "duplicate",
                "record": existing,
                "verified": True,
                "reconciled_after_timeout": False,
            }

        # 2. Deterministic Idempotency-Key
        idem_key = generate_idempotency_key(
            "create_invoice",
            vendor,
            invoice_number,
            f"{float(amount):.2f}",
        )

        # 3. Execute POST /invoices with bounded retry on 503 (before-commit)
        # Note: execute_with_retry does NOT blindly retry PostCommitTimeoutError (504).
        reconciled = False
        created_record: Optional[Dict[str, Any]] = None

        try:
            resp = execute_with_retry(
                lambda: self.client.create_invoice(
                    vendor=vendor,
                    invoice_number=invoice_number,
                    amount=amount,
                    due_date=due_date,
                    currency=currency,
                    source_document=source_document,
                    idempotency_key=idem_key,
                ),
                policy=self.policy,
                operation_name=f"create_invoice({invoice_number})",
                audit_log=self.audit_log,
            )
            created_record = resp.get("invoice", resp) if isinstance(resp, dict) else None
        except PostCommitTimeoutError:
            # 4. HTTP 504 after-commit recovery: inspect backend state BEFORE retrying!
            reconciled = True
            self.audit_log.append(
                f"create_invoice({invoice_number}): caught 504 PostCommitTimeoutError; querying backend state before retry"
            )
            committed = self.find_existing_invoice(vendor, invoice_number)
            if committed is not None:
                self.audit_log.append(
                    f"create_invoice({invoice_number}): reconciled committed record {committed.get('record_id')} without duplicate write"
                )
                created_record = committed
            else:
                # Not found in DB; safe to retry once with the same idempotency key
                resp = execute_with_retry(
                    lambda: self.client.create_invoice(
                        vendor=vendor,
                        invoice_number=invoice_number,
                        amount=amount,
                        due_date=due_date,
                        currency=currency,
                        source_document=source_document,
                        idempotency_key=idem_key,
                    ),
                    policy=self.policy,
                    operation_name=f"create_invoice_reconcile({invoice_number})",
                    audit_log=self.audit_log,
                )
                created_record = resp.get("invoice", resp) if isinstance(resp, dict) else None

        if not created_record or "record_id" not in created_record:
            raise RuntimeError(f"Invoice creation returned invalid record for {invoice_number}")

        record_id = created_record["record_id"]

        # 5. Read-after-write verification against persisted SQLite record
        persisted_resp = execute_with_retry(
            lambda: self.client.get_invoice(record_id),
            policy=self.policy,
            operation_name=f"get_invoice({record_id})",
            audit_log=self.audit_log,
        )
        persisted = persisted_resp.get("invoice", persisted_resp) if isinstance(persisted_resp, dict) else None
        if not isinstance(persisted, dict):
            raise RuntimeError(f"Read-after-write lookup failed for {record_id}")

        ok, err_msg = verify_read_after_write(
            intended_fields={
                "invoice_number": invoice_number,
                "amount": float(amount),
                "due_date": due_date,
            },
            persisted_record=persisted,
        )
        if not ok:
            raise RuntimeError(err_msg or f"Read-after-write verification failed for {record_id}")

        self.audit_log.append(
            f"record_invoice({record_id}): verified read-after-write (reconciled_504={reconciled})"
        )

        return {
            "status": "created",
            "record": persisted,
            "verified": True,
            "reconciled_after_timeout": reconciled,
        }

    def execute_payment(
        self,
        record_id: str,
        amount: float,
        approval_token: str,
        currency: str = "INR",
        approver_role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a payment strictly gated by HMAC approval verification and verified via read-after-write."""
        # 1. Non-bypassable programmatic governance gate
        self.approval_gate.verify_and_consume(
            token=approval_token,
            record_id=record_id,
            amount=float(amount),
            currency=currency,
            expected_scope="payment_submit",
            approver_role=approver_role,
        )

        # 2. Pre-write check: verify invoice state and check for existing payment
        existing_payments = execute_with_retry(
            lambda: self.client.list_payments(record_id=record_id),
            policy=self.policy,
            operation_name=f"list_payments({record_id})",
            audit_log=self.audit_log,
        )
        if existing_payments:
            return {
                "status": "already_paid",
                "payment": existing_payments[0],
                "verified": True,
            }

        # 3. Execute POST /payments with 503 retry and 504 post-commit reconciliation
        payment_record: Optional[Dict[str, Any]] = None
        reconciled = False
        try:
            resp = execute_with_retry(
                lambda: self.client.create_payment(record_id=record_id, amount=float(amount)),
                policy=self.policy,
                operation_name=f"create_payment({record_id})",
                audit_log=self.audit_log,
            )
            payment_record = resp.get("payment", resp) if isinstance(resp, dict) else None
        except PostCommitTimeoutError:
            reconciled = True
            after_payments = execute_with_retry(
                lambda: self.client.list_payments(record_id=record_id),
                policy=self.policy,
                operation_name=f"reconcile_payments({record_id})",
                audit_log=self.audit_log,
            )
            if after_payments:
                payment_record = after_payments[0]
            else:
                raise RuntimeError(f"Payment 504 timeout and no committed payment found for {record_id}")

        if not payment_record or "payment_id" not in payment_record:
            raise RuntimeError(f"Payment creation failed for {record_id}")

        payment_id = payment_record["payment_id"]

        # 4. Read-after-write verification on both payment and invoice status
        persisted_pay_resp = execute_with_retry(
            lambda: self.client.get_payment(payment_id),
            policy=self.policy,
            operation_name=f"get_payment({payment_id})",
            audit_log=self.audit_log,
        )
        persisted_payment = (
            persisted_pay_resp.get("payment", persisted_pay_resp)
            if isinstance(persisted_pay_resp, dict)
            else None
        )

        persisted_inv_resp = execute_with_retry(
            lambda: self.client.get_invoice(record_id),
            policy=self.policy,
            operation_name=f"get_invoice({record_id})",
            audit_log=self.audit_log,
        )
        persisted_invoice = (
            persisted_inv_resp.get("invoice", persisted_inv_resp)
            if isinstance(persisted_inv_resp, dict)
            else None
        )

        ok_pay, pay_err = verify_read_after_write(
            intended_fields={"record_id": record_id, "amount": float(amount)},
            persisted_record=persisted_payment,
        )
        ok_inv, inv_err = verify_read_after_write(
            intended_fields={"record_id": record_id, "status": "Paid"},
            persisted_record=persisted_invoice,
        )
        if not ok_pay:
            raise RuntimeError(pay_err or f"Payment read-after-write failed for {payment_id}")
        if not ok_inv:
            raise RuntimeError(inv_err or f"Invoice status read-after-write failed for {record_id}")

        self.audit_log.append(
            f"execute_payment({payment_id}): verified read-after-write for {record_id} (reconciled_504={reconciled})"
        )

        return {
            "status": "paid",
            "payment": persisted_payment,
            "invoice": persisted_invoice,
            "verified": True,
        }


class FinanceTools:
    """Tool wrapper exposing OperationEngine for orchestrator compatibility."""

    def __init__(self, engine: OperationEngine):
        self.engine = engine
