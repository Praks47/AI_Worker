"""Focused regression test suite for Payment Governance Boundaries and Approver Role Verification.

Verifies:
1. Direct payment requests to POST /payments without authorization fail closed (HTTP 403).
2. Missing or invalid approver identity is rejected.
3. A supervisor cannot authorize a payment requiring a finance manager (Policy 3.b).
4. A valid authorized approval still works (Policy 3.a supervisor, Policy 3.b finance manager).
5. Tampered, replayed, and transaction-mismatched tokens are rejected at API and orchestrator boundaries.
6. All rejected attempts leave payment and invoice state 100% unmutated.
"""
from __future__ import annotations

import base64
import json
import time
import pytest

from agent.finance.finance_client import FinanceClient
from agent.orchestrator import AgentOrchestrator
from agents.models import TaskStatus
from agents.reliability import GovernanceViolationError
from agents.verification import PaymentApprovalGate
from eval import db_inspect


# ---------------------------------------------------------------------------
# 1. Direct API boundary tests (POST /payments)
# ---------------------------------------------------------------------------

def test_direct_api_payment_without_authorization_rejected(client):
    """Direct HTTP POST /payments without X-Approval-Token is rejected with 403 Forbidden."""
    initial_payments = len(db_inspect.payments())
    inv_before = db_inspect.invoices_matching("Globex", "GX-7731")[0]
    assert inv_before["status"] == "Pending"

    # Direct unauthorized call
    r = client.post("/payments", json={"record_id": "INV-1002", "amount": 48200.00})
    assert r.status_code == 403
    assert "missing hmac approval token" in r.json()["detail"].lower()

    # Database state remains completely unmutated
    assert len(db_inspect.payments()) == initial_payments
    inv_after = db_inspect.invoices_matching("Globex", "GX-7731")[0]
    assert inv_after["status"] == "Pending"


def test_direct_api_payment_with_forged_token_rejected(client):
    """Direct HTTP POST /payments with a forged HMAC signature is rejected with 403 Forbidden."""
    initial_payments = len(db_inspect.payments())
    forged_token = "eyJyZWNvcmRfaWQiOiJJTlYtMTAwMiIsImFtb3VudCI6IjQ4MjAwLjAwIn0=.deadbeefcafebabe00000000"

    r = client.post(
        "/payments",
        json={"record_id": "INV-1002", "amount": 48200.00},
        headers={"X-Approval-Token": forged_token},
    )
    assert r.status_code == 403
    assert "invalid hmac signature" in r.json()["detail"].lower()
    assert len(db_inspect.payments()) == initial_payments


def test_direct_api_payment_with_tampered_amount_rejected(client):
    """Direct HTTP POST /payments where amount in token differs from body fails closed."""
    initial_payments = len(db_inspect.payments())
    gate = PaymentApprovalGate()
    # Token issued for 48,200.00
    token = gate.issue_approval_request_token("t-tamper", "INV-1002", 48200.00, "INR")

    # Body sends 40,000.00 with token issued for 48,200.00
    r = client.post(
        "/payments",
        json={"record_id": "INV-1002", "amount": 40000.00},
        headers={"X-Approval-Token": token},
    )
    # Either amount mismatch with invoice (422) or payload mismatch with token (403)
    assert r.status_code in (403, 422)
    assert len(db_inspect.payments()) == initial_payments


def test_direct_api_payment_with_transaction_mismatched_invoice_rejected(client):
    """Direct HTTP POST /payments using token issued for INV-1001 to pay INV-1002 is rejected."""
    initial_payments = len(db_inspect.payments())
    gate = PaymentApprovalGate()
    token_for_inv1001 = gate.issue_approval_request_token("t-mismatch", "INV-1001", 48200.00, "INR")

    r = client.post(
        "/payments",
        json={"record_id": "INV-1002", "amount": 48200.00},
        headers={"X-Approval-Token": token_for_inv1001},
    )
    assert r.status_code == 403
    assert "payload mismatch" in r.json()["detail"].lower()
    assert len(db_inspect.payments()) == initial_payments


def test_direct_api_payment_with_expired_token_rejected(client):
    """Direct HTTP POST /payments using an expired token fails closed."""
    initial_payments = len(db_inspect.payments())
    # Gate with 0 second TTL
    gate = PaymentApprovalGate(ttl_seconds=-10)
    token = gate.issue_approval_request_token("t-exp", "INV-1002", 48200.00, "INR")

    r = client.post(
        "/payments",
        json={"record_id": "INV-1002", "amount": 48200.00},
        headers={"X-Approval-Token": token},
    )
    assert r.status_code == 403
    assert "expired" in r.json()["detail"].lower()
    assert len(db_inspect.payments()) == initial_payments


def test_direct_api_payment_replay_rejected_by_database_nonce(client):
    """Replaying an already-consumed token at the API boundary is rejected by SQLite nonces."""
    initial_payments = len(db_inspect.payments())
    gate = PaymentApprovalGate()
    token = gate.issue_approval_request_token("t-replay", "INV-1002", 48200.00, "INR")

    # 1. First execution succeeds
    r1 = client.post(
        "/payments",
        json={"record_id": "INV-1002", "amount": 48200.00},
        headers={"X-Approval-Token": token},
    )
    assert r1.status_code == 201
    assert len(db_inspect.payments()) == initial_payments + 1

    # 2. Replay with identical token is rejected
    r2 = client.post(
        "/payments",
        json={"record_id": "INV-1002", "amount": 48200.00},
        headers={"X-Approval-Token": token},
    )
    assert r2.status_code in (403, 409)
    # Total payments remains exactly initial + 1 (no duplicate write!)
    assert len(db_inspect.payments()) == initial_payments + 1


# ---------------------------------------------------------------------------
# 2. Orchestrator boundary tests (resume_with_approval & separation of duties)
# ---------------------------------------------------------------------------

def test_orchestrator_missing_approver_identity_rejected_on_high_value_payment(client):
    """Payments >= 50,000 reject resume_with_approval when approver_role is None."""
    fc = FinanceClient(client=client)
    orchestrator = AgentOrchestrator(finance_client=fc)

    # Record Wayne invoice first (amount: 74,250.00 >= 50,000)
    rec_resp = orchestrator.run("Find the latest invoice from Wayne Enterprises and record it in the finance system.")
    assert rec_resp.success is True
    wayne_inv = db_inspect.invoices_matching("Wayne", "WE-2026-0412")[0]
    wayne_rec_id = wayne_inv["record_id"]
    assert wayne_inv["amount"] == 74250.00

    initial_payments = len(db_inspect.payments())

    # Request payment -> halts at NEEDS_APPROVAL
    pay_task = orchestrator.run(f"Pay invoice {wayne_rec_id}.")
    assert pay_task.status == TaskStatus.NEEDS_APPROVAL
    assert pay_task.approval_token is not None

    # Resume with approver_role=None (missing approver identity) -> MUST BE REJECTED
    appr_res = orchestrator.resume_with_approval(
        task_id=pay_task.task_id,
        approval_token=pay_task.approval_token,
        approved=True,
        approver_role=None,
    )
    assert appr_res.status == TaskStatus.FAILED
    assert appr_res.success is False
    assert "finance_manager" in appr_res.summary.lower()

    # Zero payments created; invoice remains not paid
    assert len(db_inspect.payments()) == initial_payments
    assert db_inspect.invoices_matching("Wayne", "WE-2026-0412")[0]["status"] != "Paid"


def test_orchestrator_supervisor_cannot_authorize_finance_manager_payment(client):
    """A supervisor cannot authorize a payment requiring a finance manager (Policy 3.b)."""
    fc = FinanceClient(client=client)
    orchestrator = AgentOrchestrator(finance_client=fc)

    rec_resp = orchestrator.run("Find the latest invoice from Wayne Enterprises and record it in the finance system.")
    wayne_inv = db_inspect.invoices_matching("Wayne", "WE-2026-0412")[0]
    wayne_rec_id = wayne_inv["record_id"]
    initial_payments = len(db_inspect.payments())

    pay_task = orchestrator.run(f"Pay invoice {wayne_rec_id}.")
    assert pay_task.status == TaskStatus.NEEDS_APPROVAL

    # Supervisor attempts to authorize 74,250.00 -> MUST BE REJECTED
    appr_res = orchestrator.resume_with_approval(
        task_id=pay_task.task_id,
        approval_token=pay_task.approval_token,
        approved=True,
        approver_role="supervisor",
    )
    assert appr_res.status == TaskStatus.FAILED
    assert appr_res.success is False
    assert "finance_manager" in appr_res.summary.lower()
    assert len(db_inspect.payments()) == initial_payments


def test_orchestrator_invalid_approver_role_rejected(client):
    """Unrecognized / invalid approver roles (e.g. 'intern', 'guest') are rejected."""
    fc = FinanceClient(client=client)
    orchestrator = AgentOrchestrator(finance_client=fc)
    initial_payments = len(db_inspect.payments())

    pay_task = orchestrator.run("Pay the Globex invoice INV-1002.")
    assert pay_task.status == TaskStatus.NEEDS_APPROVAL

    # Invalid role 'intern'
    appr_res = orchestrator.resume_with_approval(
        task_id=pay_task.task_id,
        approval_token=pay_task.approval_token,
        approved=True,
        approver_role="intern",
    )
    assert appr_res.status == TaskStatus.FAILED
    assert appr_res.success is False
    assert "invalid approver role" in appr_res.summary.lower()
    assert len(db_inspect.payments()) == initial_payments


def test_orchestrator_valid_finance_manager_approval_succeeds(client):
    """A genuine finance_manager approval for high-value payment (>= 50,000) succeeds."""
    fc = FinanceClient(client=client)
    orchestrator = AgentOrchestrator(finance_client=fc)

    rec_resp = orchestrator.run("Find the latest invoice from Wayne Enterprises and record it in the finance system.")
    wayne_inv = db_inspect.invoices_matching("Wayne", "WE-2026-0412")[0]
    wayne_rec_id = wayne_inv["record_id"]
    initial_payments = len(db_inspect.payments())

    pay_task = orchestrator.run(f"Pay invoice {wayne_rec_id}.")
    assert pay_task.status == TaskStatus.NEEDS_APPROVAL

    # Valid finance_manager authorization
    appr_res = orchestrator.resume_with_approval(
        task_id=pay_task.task_id,
        approval_token=pay_task.approval_token,
        approved=True,
        approver_role="finance_manager",
    )
    assert appr_res.status == TaskStatus.COMPLETED
    assert appr_res.success is True
    assert "paid" in appr_res.summary.lower()

    # Exactly 1 new payment created; status = Paid
    assert len(db_inspect.payments()) == initial_payments + 1
    assert db_inspect.invoices_matching("Wayne", "WE-2026-0412")[0]["status"] == "Paid"


def test_orchestrator_valid_supervisor_approval_succeeds_for_standard_payment(client):
    """A supervisor approval for standard payment (< 50,000) succeeds."""
    fc = FinanceClient(client=client)
    orchestrator = AgentOrchestrator(finance_client=fc)
    initial_payments = len(db_inspect.payments())

    pay_task = orchestrator.run("Pay the Globex invoice INV-1002.")
    assert pay_task.status == TaskStatus.NEEDS_APPROVAL

    # Valid supervisor authorization for 48,200.00
    appr_res = orchestrator.resume_with_approval(
        task_id=pay_task.task_id,
        approval_token=pay_task.approval_token,
        approved=True,
        approver_role="supervisor",
    )
    assert appr_res.status == TaskStatus.COMPLETED
    assert appr_res.success is True
    assert "paid" in appr_res.summary.lower()

    assert len(db_inspect.payments()) == initial_payments + 1
    assert db_inspect.invoices_matching("Globex", "GX-7731")[0]["status"] == "Paid"
