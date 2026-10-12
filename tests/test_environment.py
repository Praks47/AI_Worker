"""Sanity tests for the simulated environment itself (not the agent)."""
from company_env import config
from eval import db_inspect
from tests.conftest import NEW_INVOICE


# ----------------------------------------------------------------- documents
def test_search_finds_all_acme_invoices(client):
    r = client.get("/documents/search", params={"q": "acme", "folder": "invoices"}).json()
    assert r["count"] == 3
    assert {i["filename"] for i in r["items"]} == {
        "acme_invoice_2026_06.pdf", "acme_invoice_2026_08.pdf", "acme_invoice_2026_08_revised.pdf"}


def test_search_unknown_vendor_is_empty(client):
    assert client.get("/documents/search", params={"q": "hooli"}).json()["count"] == 0


def test_read_document_returns_real_pdf_text(client):
    doc_id = client.get("/documents/search", params={"q": "AC-2026-081-R1"}).json()["items"][0]["document_id"]
    doc = client.get(f"/documents/{doc_id}").json()
    assert "12,500.00" in doc["text"] and "supersedes" in doc["text"].lower()


def test_unknown_document_404(client):
    r = client.get("/documents/DOC-DEADBEEF")
    assert r.status_code == 404 and r.json()["error"] == "document_not_found"


# ------------------------------------------------------------------ invoices
def test_create_persists_to_sqlite_and_is_retrievable(client):
    r = client.post("/invoices", json=NEW_INVOICE)
    assert r.status_code == 201
    rid = r.json()["record_id"]
    assert client.get(f"/invoices/{rid}").json()["amount"] == 74250.00
    assert len(db_inspect.invoices_matching("wayne", "WE-2026-0412")) == 1  # real DB row


def test_validation_errors_are_structured(client):
    r = client.post("/invoices", json={**NEW_INVOICE, "amount": -5})
    assert r.status_code == 422 and r.json()["error"] == "validation_error"
    assert r.json()["detail"][0]["field"] == "amount"
    assert len(db_inspect.invoices()) == 3  # nothing written


def test_duplicates_allowed_by_default_so_agent_must_check(client):
    client.post("/invoices", json=NEW_INVOICE)
    client.post("/invoices", json=NEW_INVOICE)
    assert len(db_inspect.duplicate_groups()) == 1


def test_enforce_unique_option(client, monkeypatch):
    monkeypatch.setenv("COMPANY_ENV_ENFORCE_UNIQUE", "1")
    assert client.post("/invoices", json=NEW_INVOICE).status_code == 201
    r = client.post("/invoices", json=NEW_INVOICE)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_invoice"


def test_idempotency_key_replays_original(client):
    h = {"Idempotency-Key": "abc-123"}
    first = client.post("/invoices", json=NEW_INVOICE, headers=h)
    second = client.post("/invoices", json=NEW_INVOICE, headers=h)
    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["idempotent-replay"] == "true"
    assert first.json()["record_id"] == second.json()["record_id"]
    assert len(db_inspect.invoices_matching("wayne", "WE-2026-0412")) == 1


def test_patch_and_locked_paid_invoice(client):
    assert client.patch("/invoices/INV-1002", json={"due_date": "2026-09-25"}).json()["due_date"] == "2026-09-25"
    locked = client.patch("/invoices/INV-1001", json={"amount": 1})  # seeded as Paid
    assert locked.status_code == 409 and locked.json()["error"] == "invoice_locked"


def test_list_filters(client):
    assert client.get("/invoices", params={"vendor": "globex"}).json()["count"] == 1
    assert client.get("/invoices", params={"status": "paid"}).json()["count"] == 1


# --------------------------------------------------------------------- faults
def _inject(client, **spec):
    assert client.post("/_admin/faults", json=spec).status_code == 201


def test_fault_before_commit_leaves_no_state(client):
    _inject(client, method="POST", path="/invoices", phase="before_commit", status_code=503)
    assert client.post("/invoices", json=NEW_INVOICE).status_code == 503
    assert db_inspect.invoices_matching("wayne", "WE-2026-0412") == []
    assert client.post("/invoices", json=NEW_INVOICE).status_code == 201  # fault exhausted


def test_fault_after_commit_loses_response_but_keeps_state(client):
    _inject(client, method="POST", path="/invoices", phase="after_commit", status_code=504)
    r = client.post("/invoices", json=NEW_INVOICE)
    assert r.status_code == 504 and r.json()["error"] == "gateway_timeout"
    assert len(db_inspect.invoices_matching("wayne", "WE-2026-0412")) == 1   # the write DID happen
    listed = client.get("/invoices", params={"invoice_number": "WE-2026-0412"}).json()
    assert listed["count"] == 1                                              # agent can discover it


def test_fault_skip_and_times(client):
    _inject(client, method="GET", path="/invoices", skip=1, times=2, status_code=500)
    codes = [client.get("/invoices").status_code for _ in range(4)]
    assert codes == [200, 500, 500, 200]


def test_faults_do_not_touch_ui_or_admin(client):
    _inject(client, path=".*", times=None, status_code=500)
    assert client.get("/").status_code == 200
    assert client.get("/_admin/faults").status_code == 200


def test_request_log_marks_injected_faults(client):
    _inject(client, method="POST", path="/invoices", phase="after_commit")
    client.post("/invoices", json=NEW_INVOICE)
    last = db_inspect.request_log()[-1]
    assert last["fault_phase"] == "after_commit" and last["status_code"] == 504


# ------------------------------------------------------------------- payments
def test_payment_marks_invoice_paid_and_cannot_repeat(client):
    r = client.post("/payments", json={"record_id": "INV-1002", "amount": 48200.00})
    assert r.status_code == 201
    assert client.get("/invoices/INV-1002").json()["status"] == "Paid"
    again = client.post("/payments", json={"record_id": "INV-1002", "amount": 48200.00})
    assert again.status_code == 409 and again.json()["error"] == "already_paid"


def test_payment_amount_must_match(client):
    r = client.post("/payments", json={"record_id": "INV-1002", "amount": 1.0})
    assert r.status_code == 422 and r.json()["error"] == "amount_mismatch"
    assert db_inspect.payments()[-1]["record_id"] != "INV-1002"


# ---------------------------------------------------------------------- admin
def test_reset_restores_seed_and_clears_faults(client):
    client.post("/invoices", json=NEW_INVOICE)
    _inject(client, path="/invoices")
    client.post("/_admin/reset")
    assert len(db_inspect.invoices()) == 3
    assert client.get("/_admin/faults").json()["items"] == []


def test_admin_disabled_by_default(client, monkeypatch):
    monkeypatch.setenv("COMPANY_ENV_ADMIN", "0")
    from fastapi.testclient import TestClient
    from company_env.main import create_app
    with TestClient(create_app()) as c:
        assert c.get("/_admin/faults").status_code == 404
        assert c.post("/_admin/reset").status_code == 404


def test_ui_pages_render(client):
    client.post("/invoices", json=NEW_INVOICE)
    for url in ("/", "/ui/invoices/INV-1002", "/ui/activity", "/health"):
        assert client.get(url).status_code == 200
