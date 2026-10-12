"""Tests for the interactive UI demonstration routes, task execution, and reliability controls."""
from __future__ import annotations

import pytest
from eval import db_inspect


def test_ui_api_state(client):
    r = client.get("/ui/api/state")
    assert r.status_code == 200
    data = r.json()
    assert "invoices" in data
    assert "payments" in data
    assert "faults" in data
    assert "request_log" in data
    assert "task_history" in data
    assert "documents" in data
    assert len(data["invoices"]) >= 3


def test_ui_task_runner_contract_query(client):
    r = client.post("/ui/api/tasks/run", json={"prompt": "When does our contract with Acme expire?"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    assert data["success"] is True
    assert "2027-03-31" in (data["answer"] or "")
    assert data["evidence"] is not None


def test_ui_task_runner_clarification_flow(client):
    # 1. Trigger ambiguous query
    r = client.post("/ui/api/tasks/run", json={"prompt": "Record the latest invoice from Initech."})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "needs_clarification"
    assert len(data["clarification_options"]) >= 2
    task_id = data["task_id"]

    # 2. Clarify with initech_invoice_b
    r_clar = client.post(
        "/ui/api/tasks/clarify",
        json={"task_id": task_id, "selected_option": "initech_invoice_b"},
    )
    assert r_clar.status_code == 200
    res_data = r_clar.json()
    assert res_data["status"] == "completed"
    assert res_data["success"] is True
    matched = db_inspect.invoices_matching("Initech", "IN-2215")
    assert len(matched) == 1


def test_ui_task_runner_governed_payment_approval_flow(client):
    # 1. Request payment -> halts at needs_approval
    r = client.post("/ui/api/tasks/run", json={"prompt": "Pay the Globex invoice INV-1002."})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "needs_approval"
    assert data["approval_required"] is True
    token = data["approval_token"]
    task_id = data["task_id"]

    # 2. Denial flow
    r_deny = client.post(
        "/ui/api/tasks/approve",
        json={"task_id": task_id, "approval_token": token, "approved": False},
    )
    assert r_deny.status_code == 200
    assert "denied" in r_deny.json()["summary"].lower()
    assert db_inspect.invoices_matching("Globex", "GX-7731")[0]["status"] == "Pending"

    # 3. Fresh task -> Approved flow
    r2 = client.post("/ui/api/tasks/run", json={"prompt": "Pay the Globex invoice INV-1002."})
    d2 = r2.json()
    r_appr = client.post(
        "/ui/api/tasks/approve",
        json={
            "task_id": d2["task_id"],
            "approval_token": d2["approval_token"],
            "approved": True,
            "approver_role": "supervisor",
        },
    )
    assert r_appr.status_code == 200
    assert r_appr.json()["status"] == "completed"
    assert db_inspect.invoices_matching("Globex", "GX-7731")[0]["status"] == "Paid"


def test_ui_document_inspect(client):
    r_docs = client.get("/ui/api/documents")
    assert r_docs.status_code == 200
    items = r_docs.json()["items"]
    assert len(items) > 0

    # Pick wayne invoice
    wayne = next(item for item in items if "wayne" in item["filename"].lower())
    r_insp = client.post("/ui/api/documents/inspect", json={"rel_path": wayne["rel_path"]})
    assert r_insp.status_code == 200
    data = r_insp.json()
    assert data["ok"] is True
    assert "WE-2026-0412" in data["raw_text"]
    fields = data["extraction"]["fields"]
    assert fields["total_amount"]["extracted_value"] == "74250.00"
    assert fields["total_amount"]["verification_status"] == "verified"


def test_ui_create_sample_doc_and_inspect(client):
    sample_content = (
        "Vendor: Stark Industries\n"
        "Invoice Number: STK-UI-777\n"
        "Due Date: 2026-11-20\n"
        "Subtotal: INR 10,000.00\n"
        "GST (18%): INR 1,800.00\n"
        "Total Due: INR 11,800.00\n"
        "Payment Terms: Net 30\n"
    )
    r_create = client.post(
        "/ui/api/documents/create-sample",
        json={"filename": "stark_ui_test", "format": "txt", "content": sample_content},
    )
    assert r_create.status_code == 200
    rel_path = r_create.json()["rel_path"]

    r_insp = client.post("/ui/api/documents/inspect", json={"rel_path": rel_path})
    assert r_insp.status_code == 200
    data = r_insp.json()
    assert data["ok"] is True
    assert data["extraction"]["fields"]["invoice_number"]["extracted_value"] == "STK-UI-777"
    assert data["extraction"]["fields"]["total_amount"]["extracted_value"] == "11800.00"


def test_ui_record_and_patch_invoice(client):
    # Record new invoice via OperationEngine mode
    payload = {
        "mode": "engine",
        "vendor": "Cyberdyne Systems",
        "invoice_number": "CY-9901",
        "amount": 25000.00,
        "currency": "INR",
        "due_date": "2026-11-15",
        "source_document": "cyberdyne_contract.pdf",
    }
    r = client.post("/ui/api/invoices/record", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    rec_id = data["record"]["record_id"]
    assert data["read_after_write_verified"] is True

    # Duplicate attempt is prevented
    r_dup = client.post("/ui/api/invoices/record", json=payload)
    assert r_dup.status_code == 200
    assert r_dup.json()["operation_status"] == "duplicate"

    # Patch invoice
    r_patch = client.patch(f"/ui/api/invoices/{rec_id}", json={"status": "Approved"})
    assert r_patch.status_code == 200
    assert r_patch.json()["ok"] is True
    assert r_patch.json()["record"]["status"] == "Approved"


def test_ui_governance_gate_bypass_block(client):
    r_bypass = client.post(
        "/ui/api/payments/test-bypass",
        json={"record_id": "INV-1002", "amount": 48200.00, "scenario": "forged_token"},
    )
    assert r_bypass.status_code == 200
    data = r_bypass.json()
    assert data["blocked"] is True
    assert data["exception_type"] == "GovernanceViolationError"


def test_ui_fault_controls_and_reset(client):
    # Add fault rule
    r_fault = client.post(
        "/ui/api/faults",
        json={
            "method": "POST",
            "path": "/invoices",
            "phase": "before_commit",
            "status_code": 503,
            "times": 2,
        },
    )
    assert r_fault.status_code == 200

    # Clear faults
    r_clear = client.delete("/ui/api/faults")
    assert r_clear.status_code == 200

    # Reset environment
    r_reset = client.post("/ui/api/reset")
    assert r_reset.status_code == 200
    assert len(db_inspect.invoices()) == 3
