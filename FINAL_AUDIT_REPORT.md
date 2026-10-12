# FINAL PRE-DEADLINE AUDIT REPORT — AUTONOMOUS AI TASK WORKER

**Repository:** `D:\centreai`  
**Git Branch / Commit:** `master` @ `6a3a3bc`  
**Audit Date:** October 12, 2026, 22:15 IST  
**Auditor:** Autonomous AI System Auditor (DeepMind Antigravity)  
**Execution Constraint Adherence:** Zero application code modified during audit. Full repository inspection conducted.

---

## 1. Executive Summary

A comprehensive, repository-wide pre-deadline audit of the **Autonomous AI Task Worker** was conducted across all architectural layers: LLM interpretation, ReAct orchestration, document intelligence (Category A), record management (Category B), cross-application workflows (Category C), investigation and fault recovery (Category D), HMAC governance (Category E), database persistence, HTTP APIs, interactive UI, and test suites.

### Verified Strengths
1. **Core Benchmark Excellence:** The system achieves **11/11 (100.0%)** on the evaluation benchmark (`eval/run_benchmark.py`).
2. **Comprehensive Test Coverage:** The repository contains **137 passing tests and 1 skipped test** (100% pass rate in 146.97s).
3. **Zero-API-Call Default Testing:** The NLP regression test suite executes 36 intent, adversarial, mutation-safety, and reliability tests offline in 30.20s with **zero live Groq API calls**.
4. **Deterministic Fault Handling:** 503 transient errors trigger bounded exponential backoff; 504 post-commit timeouts halt blind retry and perform database reconciliation to prevent duplicate records.
5. **Fail-Closed Evidence Grounding:** Corrupted OCR amounts and ambiguous dates (`03/04/2026`) are refused/escalated rather than guessed.

### Critical Deficiencies Identified
1. **No Authorization on Public `POST /payments` (P1):** The cryptographic HMAC approval gate is implemented in `AgentOrchestrator` and `OperationEngine`, but the underlying FastAPI HTTP route `POST /payments` in `company_env/api.py` has **zero token or role verification**. Any direct API caller can execute payments without approval.
2. **Auto-Elevation of Approver Role (P1):** In `agent/orchestrator.py` line 263, if `approver_role` is omitted when resuming approval, the orchestrator automatically defaults `effective_role` to `PaymentApprovalGate.required_role(amount)`. This auto-promotes any caller to `finance_manager` for payments $\ge 50,000$, defeating separation of duties.
3. **`PaymentApprovalGate` Permits `approver_role=None` on High-Value Payments (P2):** In `agents/verification.py` line 579, the check `if req_role == "finance_manager" and approver_role not in (None, "finance_manager")` allows `None` to bypass the `finance_manager` requirement.
4. **Documentation Discrepancies (P2):** `README.md` lists an obsolete directory structure (`agent/document_intelligence/`), claims 100 tests instead of 137, omits `groq` from `pip install`, provides bash-only startup syntax that fails on PowerShell, and lacks a `.env.example` template.

---

## 2. Release Decision

### **READY WITH KNOWN LIMITATIONS**

> **Confidence Level:** High (95%).
> 
> **Rationale:** The system is **fully functional, reproducible, and demonstrable** for the benchmark submission. All 11 benchmark scenarios pass (100%), and the full test suite passes (137/137 active tests). The autonomous agent adheres strictly to the approval gate when tasks run through the Orchestrator, CLI, or UI. The P1/P2 issues relate to defense-in-depth at the raw public API boundary and role default behavior, which do not break benchmark scenarios or demonstrable worker flows, but should be addressed prior to production deployment.

---

## 3. Critical and High-Severity Findings

### Finding SEC-01 (Severity: P1 — High)
- **Title:** Unprotected Public Endpoint `POST /payments` Bypasses HMAC Approval Gate
- **File & Lines:** [`company_env/api.py#L138-L156`](file:///D:/centreai/company_env/api.py#L138-L156)
- **Execution Path:** `Client -> HTTP POST /payments -> public_api.create_payment -> SQLite write`
- **Failure Scenario:** An attacker or misconfigured client bypasses the agent and sends a direct HTTP request `POST /payments {"record_id": "INV-1002", "amount": 48200.00}`. The endpoint executes the payment immediately and marks the invoice `Paid`. No HMAC token, header, or signature is requested or verified.
- **Evidence:** Inspection of `company_env/api.py`:
  ```python
  @router.post("/payments", response_model=PaymentOut, status_code=201)
  def create_payment(body: PaymentCreate, conn=Depends(get_db)):
      inv = _invoice_or_404(conn, body.record_id)
      # Checks status and amount, but NO HMAC token or authorization check exists!
      payment_id = db.insert_payment(conn, record_id=body.record_id, amount=body.amount, currency=inv["currency"])
      conn.execute("UPDATE invoices SET status='Paid', updated_at=? WHERE record_id=?", (db.utcnow(), body.record_id))
      conn.commit()
  ```
- **Impact:** Irreversible financial payment execution without supervisor authorization via direct API access.
- **Smallest Recommended Fix:** Require an `X-Approval-Token` header on `POST /payments` and validate it against `PaymentApprovalGate.verify_and_consume` before committing to the database.
- **Status:** Confirmed. (Submission Blocker: No for agent evaluation harness; Yes for enterprise deployment).

---

### Finding SEC-02 (Severity: P1 — High)
- **Title:** Orchestrator Automatically Promotes Missing Approver Role to `finance_manager`
- **File & Lines:** [`agent/orchestrator.py#L263`](file:///D:/centreai/agent/orchestrator.py#L263)
- **Execution Path:** `UI/Caller -> resume_with_approval(task_id, token, approved=True, approver_role=None)`
- **Failure Scenario:** When a user calls `resume_with_approval` without specifying an `approver_role` on an invoice $\ge \text{INR } 50,000$ (e.g. Wayne Enterprises `INR 74,250.00`), the code executes:
  ```python
  effective_role = approver_role or PaymentApprovalGate.required_role(amount)
  ```
  The system assigns `effective_role = "finance_manager"`, thereby granting manager-level authorization on behalf of any unauthenticated caller or junior supervisor.
- **Impact:** Defeats Policy 3.b segregation of duties; high-value payments can be approved without proving manager credentials.
- **Smallest Recommended Fix:** Default `effective_role = approver_role or "supervisor"`. If the payment requires `finance_manager` and `approver_role` is not explicitly `"finance_manager"`, reject with `GovernanceViolationError`.
- **Status:** Confirmed. (Submission Blocker: No for current benchmark scenarios where supervisor approval is exercised on Globex `48,200 < 50,000`; Yes for Policy 3.b strict compliance).

---

## 4. Full Findings Register

| ID | Severity | Subsystem | File & Lines | Description | Impact | Status |
| :--- | :---: | :--- | :--- | :--- | :--- | :---: |
| **SEC-01** | **P1** | Security / API | [`company_env/api.py#L138`](file:///D:/centreai/company_env/api.py#L138) | Public `POST /payments` endpoint lacks HMAC token verification | Direct API calls can execute payments without approval | Confirmed |
| **SEC-02** | **P1** | Security / Agent | [`agent/orchestrator.py#L263`](file:///D:/centreai/agent/orchestrator.py#L263) | `resume_with_approval` auto-promotes missing role to required role | Bypasses Policy 3.b separation of duty for payments $\ge 50,000$ | Confirmed |
| **SEC-03** | **P2** | Security / Gate | [`agents/verification.py#L579`](file:///D:/centreai/agents/verification.py#L579) | `verify_and_consume` permits `approver_role=None` when `req_role == 'finance_manager'` | Direct calls to `OperationEngine.execute_payment(..., approver_role=None)` bypass manager check | Confirmed |
| **SEC-04** | **P2** | Security / Auth | `company_env/api.py`, `ui.py` | Complete absence of authentication/session controls across all HTTP routes | Any local network client can reset DB or execute actions | Confirmed |
| **DOC-01** | **P2** | Documentation | [`README.md#L130-L150`](file:///D:/centreai/README.md#L130-L150) | Directory structure in README lists nonexistent paths (`agent/document_intelligence/`) | Developer confusion; paths mismatch actual code | Confirmed |
| **DOC-02** | **P2** | Documentation | [`README.md#L184`](file:///D:/centreai/README.md#L184) | Setup command omits `groq`, `python-dotenv`, `pillow` | Fresh `pip install` following README fails at runtime | Confirmed |
| **DOC-03** | **P2** | Documentation | Root directory | Missing `.env.example` file | Users unaware of `GROQ_API_KEY` configuration format | Confirmed |
| **OPS-01** | **P3** | Operational | [`README.md#L205`](file:///D:/centreai/README.md#L205) | Windows PowerShell syntax incompatible with `COMPANY_ENV_ADMIN=1 uvicorn ...` | Startup command fails on Windows default terminal | Confirmed |
| **OPS-02** | **P3** | Operational | Pytest output | `StarletteDeprecationWarning` regarding `httpx` with `starlette.testclient` | Console noise during test execution | Confirmed |
| **DOC-04** | **P3** | Documentation | [`README.md#L94`](file:///D:/centreai/README.md#L94) | Outdated test count: claims "100 passing tests" instead of 137 | Inaccurate claim in documentation | Confirmed |

---

## 5. Test Execution Table

All tests were independently executed and recorded during this audit:

| Test Command / Suite | Mode | Tests Run | Passed | Failed | Skipped | Runtime | Observed Output Summary |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| `& D:\centreai\.venv\Scripts\python.exe -m pytest tests/test_nlp_regression.py -v` | Offline Mocked | 37 | 36 | 0 | 1 | 30.20s | All 14 intents, prompt injection, and mutation-safety tests passed. Live test cleanly skipped. |
| `& D:\centreai\.venv\Scripts\python.exe -m pytest tests/test_llm_interpreter.py -v` | Offline Mocked | 11 | 11 | 0 | 0 | 11.02s | Verified missing API key handling, malformed JSON, and prompt injections. |
| `& D:\centreai\.venv\Scripts\python.exe -m eval.run_benchmark` | Simulated Integration | 11 | 11 | 0 | 0 | 59.85s | **11/11 (100.0%)** passed across Categories A-E. Verified 503 retry and 504 reconciliation. |
| `& D:\centreai\.venv\Scripts\python.exe -m pytest` | Complete Suite | 138 | 137 | 0 | 1 | 146.97s | Entire repository test suite passes cleanly with zero failures. |
| `& D:\centreai\.venv\Scripts\python.exe main.py "When does our contract with Acme expire?"` | End-to-End CLI | 1 | 1 | 0 | 0 | 5.82s | Clean JSON response with expiry date (`2027-03-31`), line grounding, and snippet. |
| `& D:\centreai\.venv\Scripts\python.exe -m scripts.setup_env` | Environment Setup | 1 | 1 | 0 | 0 | 2.65s | Generated 14 documents and seeded database cleanly. |

---

## 6. Subsystem Coverage Matrix

| Subsystem | Audit Status | Implementation Reality | Gaps / Limitations |
| :--- | :---: | :--- | :--- |
| **LLM Interpretation** | **Verified** | Groq SDK (`qwen/qwen3.8-27b`), Pydantic `TaskPlan` schema, regex injection pre-check, low-confidence demotion ($< 0.50$). | Requires valid `GROQ_API_KEY` for live mode. Falls back to offline mock in tests. |
| **Document Extraction** | **Verified** | PyMuPDF with text sufficiency check; automatic OCR rasterization for scanned PDFs; TXT, JSON, CSV, XLSX, DOCX, images supported. | Tables in PDFs rely on text line extraction rather than visual table border segmentation. |
| **Evidence Grounding** | **Verified** | Verbatim substring evidence matching against physical page coordinates, line numbers, and Excel cell coordinates (`[Sheet!Cell]`). | None observed. Grounding verification fails closed on mismatch. |
| **Mutation Safety** | **Verified** | Only `RECORD_INVOICE` mutates DB; user prohibitions (`"do not record"`) prevent writes; read queries leave SQLite unchanged. | Public HTTP endpoint `POST /payments` is unprotected if hit directly. |
| **Payment Governance** | **Partially Gated** | Non-bypassable HMAC-SHA256 signature, nonce replay protection, payload binding (record ID, amount, currency). | Approver role auto-elevated if omitted; `POST /payments` missing API-level gate. |
| **Database & Idempotency** | **Verified** | SQLite with PRAGMA foreign keys; check-before-create; SHA-256 idempotency key prevents duplicates on replay. | Single SQLite file without distributed locking (appropriate for demo/benchmark). |
| **Fault Recovery** | **Verified** | 503 before-commit retried with bounded exponential backoff; 504 after-commit halts retry and reconciles via `GET` lookup. | 504 timeout relies on status lookup; edge cases with partial network partitions tested via fault injector. |
| **Interactive UI** | **Verified** | FastAPI + Jinja2 + server-rendered and JSON API; task runner with clarification modal, approval modal, document inspector, fault toggle. | No authentication on `/ui/api/reset` or task endpoints. |
| **Documentation** | **Deficient** | High-level conceptual explanations are thorough and articulate. | Directory paths, setup dependencies, and Windows CLI syntax are inaccurate. |

---

## 7. Claims That Could Not Be Verified

1. **`agent/document_intelligence/` Architecture:**
   - *Claim:* `README.md` lines 130-140 describe a folder structure `agent/document_intelligence/` containing `discovery.py`, `extractors/`, `models.py`, `parser.py`, `registry.py`, `retrieval.py`, and `verification.py`.
   - *Reality:* That directory does not exist. The actual implementation resides in `agents/extractor.py`, `agents/models.py`, `agents/reliability.py`, and `agents/verification.py`.
2. **"100 Passing Tests":**
   - *Claim:* `README.md` line 94 claims the test suite has 100 passing tests.
   - *Reality:* The actual test suite currently has **137 passing tests and 1 skipped test**.
3. **Stand-Alone `pip install` Reproducibility:**
   - *Claim:* `pip install` command in `README.md` sets up all dependencies.
   - *Reality:* It omits `groq` and `python-dotenv`. A clean environment following only the README command fails on importing `GroqTaskInterpreter`.

---

## 8. Concrete Submission Blockers

| Blocker ID | Description | Why It Blocks / Remediation |
| :--- | :--- | :--- |
| **None for Demo** | **No blocking failures prevent submission for benchmark evaluation.** | All 11 benchmark scenarios pass; 137 tests pass; CLI works; UI works. |
| **Advisory 1** | Missing `.env.example` | Reviewers will not know what environment variables to supply without inspecting source code. Create `.env.example`. |
| **Advisory 2** | Inaccurate `README.md` setup and directory tree | Reviewers attempting to read source files or follow setup will hit missing modules or wrong paths. Update `README.md`. |

---

## 9. Prioritized Fix Plan

### Tier 1: Immediate Safety & Governance Fixes (P1)
1. **Fix Approver Role Auto-Promotion in `agent/orchestrator.py` (`SEC-02`):**
   ```python
   # Line 263 of agent/orchestrator.py
   # CHANGE FROM:
   effective_role = approver_role or PaymentApprovalGate.required_role(amount)
   # CHANGE TO:
   effective_role = approver_role or "supervisor"
   ```
2. **Fix `PaymentApprovalGate` High-Value Check in `agents/verification.py` (`SEC-03`):**
   ```python
   # Line 579 of agents/verification.py
   # CHANGE FROM:
   if req_role == "finance_manager" and approver_role not in (None, "finance_manager"):
   # CHANGE TO:
   if req_role == "finance_manager" and approver_role != "finance_manager":
   ```
3. **Protect `POST /payments` at HTTP Route Boundary (`SEC-01`):**
   Add `approval_token: Optional[str] = Header(None)` to `POST /payments` and validate with `PaymentApprovalGate`.

### Tier 2: Documentation & Developer Experience Fixes (P2)
1. **Create `.env.example` (`DOC-03`):**
   ```env
   GROQ_API_KEY=your_groq_api_key_here
   COMPANY_ENV_ADMIN=1
   COMPANY_ENV_DATA_DIR=data
   COMPANY_ENV_DB=data/finance.db
   RUN_LIVE_GROQ_TESTS=0
   ```
2. **Correct `README.md` (`DOC-01`, `DOC-02`, `DOC-04`):**
   - Update `pip install` to include `groq python-dotenv pillow`.
   - Update directory tree to reflect `agents/` and `agent/finance/`.
   - Update test count to 137.
   - Add PowerShell startup instructions: `$env:COMPANY_ENV_ADMIN="1"; uvicorn company_env.main:app --port 8000`.

---

## 10. Final Verification Checklist & Minimal Demo Sequence

### Verification Commands
```powershell
# 1. Generate clean environment and seed data
& D:\centreai\.venv\Scripts\python.exe -m scripts.setup_env

# 2. Run offline NLP regression suite (0 API calls, ~30s)
& D:\centreai\.venv\Scripts\python.exe -m pytest tests/test_nlp_regression.py -v

# 3. Run complete test suite (137 passed, ~2.5m)
& D:\centreai\.venv\Scripts\python.exe -m pytest

# 4. Run the comprehensive evaluation benchmark (11/11 passed, ~1m)
& D:\centreai\.venv\Scripts\python.exe -m eval.run_benchmark
```

### Deterministic Demonstration Sequence for Evaluators
1. **Information Retrieval (Category A):**
   ```powershell
   & D:\centreai\.venv\Scripts\python.exe main.py "When does our contract with Acme expire?"
   ```
   *Expected Outcome:* Status `completed`, Answer reports `2027-03-31` from `acme_msa_2025.pdf` with line grounding.
2. **Autonomous Record Creation (Category B & C):**
   ```powershell
   & D:\centreai\.venv\Scripts\python.exe main.py "Find the latest invoice from Wayne Enterprises and record it in the finance system."
   ```
   *Expected Outcome:* Records `WE-2026-0412` (`INV-1004`) for `INR 74,250.00`, verified via read-after-write.
3. **Ambiguity Resolution (Category D):**
   ```powershell
   & D:\centreai\.venv\Scripts\python.exe main.py "Record the latest invoice from Initech."
   ```
   *Expected Outcome:* Status `needs_clarification`, presents candidates `initech_invoice_a.pdf` and `initech_invoice_b.pdf`.
4. **Governed Payment Execution (Category E):**
   ```powershell
   & D:\centreai\.venv\Scripts\python.exe main.py "Pay the Globex invoice INV-1002."
   ```
   *Expected Outcome:* Status `needs_approval`, outputs signed HMAC approval token and halts without writing payment.
5. **Interactive Web Dashboard:**
   ```powershell
   $env:COMPANY_ENV_ADMIN="1"
   & D:\centreai\.venv\Scripts\python.exe -m uvicorn company_env.main:app --port 8000
   ```
   *Expected Outcome:* Navigate to `http://localhost:8000` to execute natural language tasks, inspect documents, test fault injection, and review the live request log.
