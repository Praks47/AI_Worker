# Autonomous AI Task Worker

An autonomous AI task worker prototype that executes multi-step natural language business tasks across contract inquiry, invoice record processing, fault recovery, and approval-gated payments against a simulated enterprise environment (FastAPI + SQLite + real PDF/DOCX/XLSX documents + HTTP fault injection).

The system integrates genuine LLM-based natural language understanding via the Groq API (`qwen/qwen3.8-27b`) with a deterministic, fail-closed reliability and governance execution engine.

---

## 1. Project Overview

### The Problem
Automating accounts payable and vendor contracts with generative AI carries substantial risk when language models are allowed direct, unconstrained access to modify database records or disburse funds. LLMs suffer from hallucinations, non-deterministic outputs, vulnerabilities to prompt injection, and poor handling of network or database faults (such as timeouts that leave transactions in indeterminate states).

### Intended Users
- **Enterprise Operations & Finance Teams**: Requiring audited, grounded automation of invoice entry and payment requests.
- **AI Systems Engineers & Evaluators**: Benchmarking autonomous agent autonomy, reliability under network faults, and adherence to security policies.

### Core Value Proposition
This task worker decouples **Intent Interpretation** from **Deterministic Execution**:
1. An LLM parses unstructured user commands into structured, validated plans.
2. A deterministic execution engine rigorously enforces document workspace isolation, 5-stage evidence grounding, check-before-create deduplication, exponential backoff retries, after-commit reconciliation, read-after-write verification, and cryptographic HMAC approval gates before irreversible state changes.

### Benchmark Category Coverage
The worker fulfills five benchmark evaluation categories:
- **Category A — Information Retrieval & Extraction**: Autonomously discover documents across formats (PDF, scanned OCR, DOCX, XLSX, CSV, images), extract facts with Decimal precision, and verify verbatim text grounding.
- **Category B — Data Entry & Record Management**: Create and manage persistent database records via public HTTP APIs with idempotency keys and read-after-write database verification.
- **Category C — Cross-Application Workflows**: Coordinate actions across document repositories and APIs, resolving superseded invoice revisions (e.g. prioritizing `AC-2026-081-R1` over obsolete `AC-2026-081`).
- **Category D — Investigation & Fault Handling**: Detect and resolve missing documents, malformed OCR amounts, ambiguous dates (`03/04/2026` vs `04/03/2026`), HTTP 503 before-commit faults, and HTTP 504 after-commit gateway timeouts without duplicate records.
- **Category E — Approval-Gated Actions**: Enforce non-bypassable cryptographic HMAC authorization gates for payments, requiring explicit supervisor authorization and strictly escalating amounts $\ge 50,000$ to finance manager review.

---

## 2. Key Features

| Capability | Status | Description |
| :--- | :---: | :--- |
| **Natural Language Task Interpretation** | **Implemented** | LLM-based intent routing using Groq API (`qwen/qwen3.8-27b`) with Pydantic structured output validation and prompt-injection defense. |
| **Workspace Containment** | **Implemented** | Strict file resolution preventing directory traversal out of the designated document store (`data/documents`). |
| **Multi-Format Document Extraction** | **Implemented** | Format adapters for PDF (`pymupdf`), DOCX (`python-docx`), XLSX (`openpyxl`), CSV, text, and raster image OCR fallback (`pillow`). |
| **5-Stage Fail-Closed Grounding** | **Implemented** | Validates text density, positive amounts, currency domains, verbatim substring evidence, and cross-source reconciliation. |
| **Deduplication & Idempotency** | **Implemented** | Check-before-create queries and deterministic SHA-256 idempotency keys (`idemp_<hash>`) preventing double posting. |
| **Differentiated Fault Recovery** | **Implemented** | Distinguishes HTTP 503 transient failures (retry with exponential backoff & jitter) from HTTP 504 gateway timeouts (halting blind retries and reconciling committed records). |
| **Read-After-Write Verification** | **Implemented** | Direct `GET` verification verifying exact field matching, Decimal precision, and confirming unrelated records are untouched. |
| **Cryptographic Payment Governance** | **Implemented** | Irreversible payment endpoint (`POST /payments`) gated by signed HMAC tokens with expiration, exact transaction binding, database nonce replay tracking, and Separation-of-Duties role enforcement. |
| **Interactive Web UI & Real-Time Log** | **Implemented** | FastAPI/Jinja2 dashboard with interactive task runner, clarification/approval modals, document explorer, fault injector, and live SQLite activity stream. |

---

## 3. Architecture

### Component Architecture

```mermaid
flowchart TD
    User([User / Browser UI / API Client]) --> UI[FastAPI Web Interface\ncompany_env.ui]
    UI --> Interp[Groq Task Interpreter\nqwen/qwen3.8-27b + Pydantic Schema]
    
    Interp -- Validated TaskPlan --> Orch[Agent Orchestrator\nagent.orchestrator]
    
    subgraph Perception ["Document Intelligence (Category A & C)"]
        Orch --> DocTools[DocumentTools]
        DocTools --> Discovery[File Discovery & Path Containment\nagents.extractor]
        DocTools --> ExtractorRegistry[Format Extractor Registry\nPDF / OCR / DOCX / XLSX / CSV]
        DocTools --> VerifEngine[5-Stage Verification Engine\nagents.verification]
        DocTools --> NetTerms[Contract Net Terms Derivation]
    end

    subgraph MutationEngine ["Deterministic Operation Engine (Category B & D)"]
        Orch --> FinTools[FinanceTools\nagent.finance.operation_engine]
        FinTools --> OpEngine[OperationEngine State Machine]
        OpEngine --> CheckDup[Check-Before-Create Query]
        OpEngine --> Backoff[Bounded Backoff & Jitter\n503 Transient Retry]
        OpEngine --> Reconcile[Reconciliation Engine\n504 Gateway Timeout Resolution]
        OpEngine --> ReadAfterWrite[Read-After-Write Verification\nDecimal Precision Check]
    end

    subgraph SecurityGate ["Payment Governance Boundary (Category E)"]
        FinTools --> GateVerifier[PaymentApprovalGate\nagents.verification]
        GateVerifier -- HMAC Verification --> TokenCheck{Valid Signature,\nRole & Scope?}
        TokenCheck -- Invalid/Expired/Role Mismatch --> ErrForbidden[403 Forbidden / Denied]
        TokenCheck -- Valid Signed Token --> AllowPayment[Pass Headers to Client]
    end

    subgraph EnterpriseBoundary ["Simulated Enterprise Environment (company_env)"]
        AllowPayment --> FinClient[FinanceClient / DirectEnvClient]
        OpEngine --> FinClient
        FinClient --> PublicAPI[Public FastAPI Endpoints\n/documents, /invoices, /payments]
        PublicAPI --> FaultMiddleware[Fault Injection Middleware\n503 before-commit / 504 after-commit]
        PublicAPI --> ReplayDB[(consumed_approval_tokens Table)]
        FaultMiddleware --> SQLite[(SQLite Database\ndata/finance.db)]
    end
```

### Execution Flow & Decision Lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI as Web Dashboard
    participant LLM as Groq Task Interpreter
    participant Orch as Agent Orchestrator
    participant Doc as Document Intelligence
    participant Engine as Operation Engine
    participant Gate as Approval Gate
    participant API as Public Enterprise API
    participant DB as SQLite Database

    User->>UI: Enter natural language prompt
    UI->>LLM: interpret(prompt)
    LLM-->>UI: Validated TaskPlan (Intent, Entities, Mutating Flag)
    
    alt Prompt Injection / Security Violation
        UI-->>User: Refusal: UNSUPPORTED_TASK (Fail-Closed)
    else Ambiguous Request (e.g. Multiple Invoices Found)
        UI->>Orch: run(plan)
        Orch->>Doc: discover_files() & extract_fields()
        Doc-->>Orch: Multiple candidate invoices detected
        Orch-->>UI: Status: NEEDS_CLARIFICATION (Options listed)
        User->>UI: Submit selected candidate
        UI->>Orch: resume_with_clarification(task_id, choice)
    else Read-Only Contract Query
        UI->>Orch: run(plan)
        Orch->>Doc: read_document() & extract_facts()
        Doc-->>Orch: Grounded fact (e.g. Expiry: 2027-03-31)
        Orch-->>UI: Status: COMPLETED (Audit log + evidence)
    else Invoice Recording Workflow
        UI->>Orch: run(plan)
        Orch->>Doc: extract_fields() & 5-stage verification
        Doc-->>Orch: Verified Invoice Data
        Orch->>Engine: execute_record_invoice(invoice_data)
        Engine->>API: GET /invoices (Check-before-create)
        alt Duplicate Found
            Engine-->>Orch: DUPLICATE_PREVENTED (Skip POST)
        else Fresh Invoice
            Engine->>API: POST /invoices (Idempotency Key)
            alt Injected 503 Fault
                API-->>Engine: 503 Injected Fault (Before Commit)
                Engine->>Engine: Bounded backoff retry with jitter
                Engine->>API: POST /invoices (Retry)
            else Injected 504 Timeout
                API-->>Engine: 504 Timeout (After Commit)
                Engine->>Engine: Enter RECONCILING state (Halt blind retry)
                Engine->>API: GET /invoices (Reconcile persistence)
            end
            API->>DB: Commit record
            Engine->>API: GET /invoices/{record_id} (Read-after-write)
            API-->>Engine: Verified persistent record
            Engine-->>Orch: Operation SUCCEEDED
        end
        Orch-->>UI: Status: COMPLETED
    else Payment Workflow (Approval-Gated)
        UI->>Orch: run(plan)
        Orch->>Gate: issue_token(record_id, amount, currency)
        alt Amount >= 50,000 INR
            Gate-->>Orch: Policy 3.b: Requires 'finance_manager' role
        else Amount < 50,000 INR
            Gate-->>Orch: Policy 3.a: Requires 'supervisor' role
        end
        Orch-->>UI: Status: NEEDS_APPROVAL (HMAC token, amount, required role)
        User->>UI: Approve payment with designated role
        UI->>Orch: resume_with_approval(task_id, token, approved=True, approver_role)
        Orch->>API: POST /payments (Header: X-Approval-Token, X-Approver-Role)
        API->>Gate: verify_token(token, role, transaction_details)
        API->>DB: Check & insert nonce in consumed_approval_tokens
        API->>DB: Insert payment & mark invoice Paid
        API-->>Orch: Payment 201 Created
        Orch-->>UI: Status: COMPLETED (Payment recorded)
    end
```

### Component Breakdown
1. **Groq Task Interpreter (`agent/task_interpreter.py`)**: Connects to the Groq API to parse natural language requests. Employs regex pre-filtering for prompt injection defense, extracts structured entities (vendor, record ID, invoice number), and outputs a validated Pydantic `TaskPlan`.
2. **Agent Orchestrator (`agent/orchestrator.py`)**: Coordinates execution across tools. Maintains in-memory session task state, orchestrates clarification resumption and human-in-the-loop approval workflows, and manages the ReAct loop.
3. **Document Intelligence (`agents/extractor.py`, `agents/verification.py`)**: Discovers local documents with directory containment checks, extracts metadata across formats, runs OCR fallbacks, and executes 5-stage verification (density, domain ranges, evidence substring match, cross-source revision resolution).
4. **Operation Engine (`agent/finance/operation_engine.py`)**: Executes mutating finance actions. Implements check-before-create lookups, deterministic idempotency keys (`idemp_<sha256>`), bounded retry with jitter for HTTP 503, reconciliation for HTTP 504 gateway timeouts, and decimal precision read-after-write verification.
5. **Cryptographic Payment Approval Gate (`agents/verification.py`, `company_env/api.py`)**: Enforces cryptographic HMAC-SHA256 tokens bound to specific invoice IDs, amounts, currencies, and approver roles. The raw API endpoint checks signature validity, expiration, and database-level nonce deduplication.
6. **Simulated Company Application (`company_env/`)**: A FastAPI server running against SQLite (`data/finance.db`), with configurable HTTP fault injection middleware (`before_commit` and `after_commit`), an interactive web UI, and public APIs.

---

## 4. Important Technical & Design Decisions

### 1. Separation of Intent (LLM) and Execution (Deterministic Engine)
- **Rationale**: LLMs are creative and non-deterministic; financial databases require strict atomicity, idempotency, and auditability.
- **Implementation**: The LLM is restricted to analyzing instructions and producing a structured plan. It has zero direct access to database connections, SQL queries, or network sockets. Deterministic code enforces whether and how any database mutation occurs.
- **Trade-Off**: The system cannot perform arbitrary novel workflows not anticipated in tool declarations; in return, it provides complete mathematical guarantees against hallucinations writing to the database.

### 2. Structured Output with Schema Validation
- **Rationale**: Free-form LLM outputs require fragile string parsing and are susceptible to subtle format drift.
- **Implementation**: Uses Pydantic schemas with strict typing (`TaskIntent`, `is_mutating`, `confidence`) and system-prompt constraints. Inputs that do not conform to schema or confidence thresholds are rejected fail-closed.

### 3. Differentiated Fault Handling (503 vs 504)
- **Rationale**: Blind retries upon receiving network errors cause duplicate billing and race conditions.
- **Implementation**:
  - `HTTP 503 (Before Commit)`: The simulated server failed prior to executing SQLite transactions. Classified as `DEFINITELY_NOT_COMMITTED` and retried safely with bounded exponential backoff and randomized jitter.
  - `HTTP 504 (After Commit)`: The server committed the record to SQLite, but the network connection timed out before the HTTP 201 response reached the client. Classified as `OUTCOME_UNKNOWN`. Blind retries are halted immediately; the engine transitions to `RECONCILING`, searches the public `/invoices` API, verifies persistence, and marks the task succeeded without duplicate records.

### 4. Non-Bypassable Cryptographic Approval Gates
- **Rationale**: An AI agent must never have autonomous authority to disburse company funds.
- **Implementation**: The `POST /payments` endpoint requires a valid HMAC-SHA256 signature generated with a shared server secret (`COMPANY_ENV_PAYMENT_SECRET`). Tokens are cryptographically bound to transaction parameters (`record_id`, `amount`, `currency`), carry a 15-minute expiration, and are recorded in SQLite (`consumed_approval_tokens`) upon use to prevent replay attacks.
- **Separation of Duties (Policy 3.b)**: Invoices $\ge \text{INR } 50,000$ strictly require `finance_manager` approval; authorization attempts by standard supervisors are rejected.

### 5. Simulated Enterprise Environment
- **Rationale**: Real enterprise ERP and banking APIs do not permit automated fault injection testing (e.g. inducing network partitions exactly after a SQL commit).
- **Implementation**: A local FastAPI service hosting SQLite and real business documents mimics an enterprise AP system, allowing rigorous verification of fault tolerance and boundary enforcement.

---

## 5. Technology Stack & External Services

| Component | Technology / Service | Role / Purpose |
| :--- | :--- | :--- |
| **Language & Runtime** | Python 3.11+ | Core implementation runtime. |
| **Web Framework & API** | FastAPI, Starlette, Uvicorn | Public REST endpoints, UI routing, middleware. |
| **Database** | SQLite 3 | Relational database (`invoices`, `payments`, `consumed_approval_tokens`, `request_log`). |
| **LLM Provider** | Groq Cloud API | Natural language task interpretation. |
| **Default Model** | `qwen/qwen3.8-27b` | Primary structured output interpretation model. |
| **Fallback Model** | `openai/gpt-oss-120b` | Backup model configuration. |
| **Document Processing** | PyMuPDF (`fitz`), `python-docx`, `openpyxl`, `Pillow` | PDF parsing, Word/Excel extraction, image OCR handling. |
| **Validation & Typing** | Pydantic v2 | Schema enforcement, request validation, structured plans. |
| **Templates & UI** | Jinja2, HTML5, Vanilla JavaScript | Browser dashboard and interactive task runner. |
| **Testing & Evaluation** | Pytest, HTTPX | Automated test suite and 11-scenario evaluation scorecard. |

### Network & Credential Requirements
- **Local Components**: The database, FastAPI server, document extractors, verification engine, and deterministic operations run entirely offline and locally.
- **External Network Access**: Calling the live task interpreter requires an outbound HTTPS connection to `api.groq.com` and a valid `GROQ_API_KEY`.
- **Offline / Deterministic Testing**: The full test suite runs locally with mocked LLM responses by default (`RUN_LIVE_GROQ_TESTS=0`), consuming zero external API quota during continuous integration.

---

## 6. Prerequisites

- **Operating System**: Windows 10/11, Linux, or macOS (commands below are optimized for Windows PowerShell).
- **Python**: Python 3.11 or higher (`python --version`).
- **Groq API Key**: A free or paid API key from [Groq Console](https://console.groq.com) (only required for live natural language queries).

---

## 7. Setup & Installation

Follow these exact steps in Windows PowerShell:

### 1. Clone the Repository
```powershell
git clone <YOUR_REPOSITORY_URL>
cd centreai
```

### 2. Create and Activate Virtual Environment
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```
*(If script execution is disabled on PowerShell, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first).*

### 3. Install Dependencies
```powershell
pip install -e .
# Or install directly via pip:
pip install fastapi "uvicorn[standard]" pymupdf openpyxl python-docx httpx pytest pydantic jinja2 groq python-dotenv pillow
```

### 4. Configure Environment Variables
Copy the sample environment file:
```powershell
Copy-Item .env.example .env
```
Open `.env` in an editor and configure the keys:
- `GROQ_API_KEY`: Set your personal API key (e.g. `gsk_...`). Leave as placeholder if only running offline tests.
- `COMPANY_ENV_PAYMENT_SECRET`: Shared secret used for HMAC approval tokens (defaults to development key).
- `RUN_LIVE_GROQ_TESTS`: Set to `0` (default) for fast offline testing; set to `1` to run live Groq tests.
- `COMPANY_ENV_ADMIN`: Set to `1` to enable administrative inspection endpoints.

### 5. Generate Synthetic Data and Seed Database
Run the environment setup script to create sample invoices (PDFs, DOCX, XLSX) and initialize the SQLite schema:
```powershell
python -m scripts.setup_env
```
*Expected output: Generates sample documents in `data/documents/` and seeds `data/finance.db`.*

---

## 8. Running the Application

### Start the Server
Start the simulated company backend and interactive dashboard:
```powershell
$env:COMPANY_ENV_ADMIN="1"
uvicorn company_env.main:app --host 127.0.0.1 --port 8000 --reload
```

### Access Endpoints
Once started, confirm the application is running by opening the following URLs in your browser:
- **Interactive Web Dashboard**: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- **Live Activity Stream**: [http://127.0.0.1:8000/ui/activity?live=1](http://127.0.0.1:8000/ui/activity?live=1)
- **Swagger / OpenAPI Documentation**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Health Check**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) (returns `{"status":"ok"}`)

---

## 9. Example Tasks & Demonstration Workflows

You can execute these representative business instructions either through the **Task Runner** interface at [http://127.0.0.1:8000](http://127.0.0.1:8000) or programmatically via `AgentOrchestrator`:

### Scenario 1: Contract Expiry Retrieval (Category A — Read-Only)
- **Prompt**: `"When does our contract with Acme expire?"`
- **Expected Behavior**: Reads `data/documents/contracts/acme_msa.pdf`, extracts contract terms, grounds evidence verbatim, and returns the expiration date (`2027-03-31`). The database remains completely unaltered.

### Scenario 2: Invoice Record Processing (Category B — Mutating)
- **Prompt**: `"Find the latest invoice from Wayne Enterprises and record it in the finance system."`
- **Expected Behavior**: Discovers `wayne_invoice_0412.pdf`, extracts invoice number (`WE-2026-0412`), total amount, and due date. Verifies ground truth evidence, checks database for existing entries, posts via `POST /invoices` with an idempotency key, and performs read-after-write verification.

### Scenario 3: Resolving Superseded Revisions (Category C — Cross-Document)
- **Prompt**: `"Record the latest Acme invoice and tell me what's due."`
- **Expected Behavior**: Identifies both `acme_invoice_081.pdf` and its revised replacement `acme_invoice_081_r1.pdf`. Reconstructs the revision graph, detects that `R1` supersedes the original, and records only the authoritative revision `AC-2026-081-R1`.

### Scenario 4: Ambiguous Request Resolution (Category D — Clarification)
- **Prompt**: `"Record the latest invoice from Initech."`
- **Expected Behavior**: Discovers two competing candidates (`initech_inv_march.pdf` and `initech_inv_apr.pdf`). Rather than guessing, halts execution with status `NEEDS_CLARIFICATION` and presents the options to the user. Resuming with the chosen candidate completes the record.

### Scenario 5: Fault Injection & Timeout Recovery (Category D — Reliability)
- **Configuration**: Trigger an HTTP 504 fault via the dashboard fault panel on `POST /invoices`.
- **Prompt**: `"Find the latest invoice from Wayne Enterprises and record it in the finance system."`
- **Expected Behavior**: Receives HTTP 504 (timeout after commit). Halts blind retries, transitions to `RECONCILING`, searches the `/invoices` API, confirms the record was successfully committed, and marks the task succeeded with **zero duplicate records**.

### Scenario 6: Approval-Gated Payment (Category E — Governance)
- **Prompt**: `"Pay the Globex invoice INV-1002."`
- **Expected Behavior**:
  1. Detects an irreversible financial mutation.
  2. Issues an HMAC payment approval token bound to invoice `INV-1002` (Amount: INR 48,200).
  3. Halts with status `NEEDS_APPROVAL`, displaying token, transaction details, and requiring `supervisor` authorization.
  4. Resuming with supervisor approval submits `POST /payments` with `X-Approval-Token`. The endpoint cryptographically verifies the token, records the nonce, and updates invoice status to `Paid`.

---

## 10. Testing & Evaluation

### 1. Run the Full Test Suite
Executes all 149 automated unit, integration, and security tests:
```powershell
python -m pytest
```
*Current Verified Result: **148 Passed, 1 Skipped** in ~200 seconds.*
*(Note: 1 test in `tests/test_nlp_regression.py` is skipped by default when running in offline mode `RUN_LIVE_GROQ_TESTS=0` to preserve external API quota).*

### 2. Run Focused Security & Payment Governance Tests
Verifies raw API authorization barriers, token forgery detection, tampering, expiration, database nonce replay prevention, and separation-of-duties enforcement:
```powershell
python -m pytest tests/test_payment_governance_boundaries.py -v
```
*Current Verified Result: **11 / 11 Passed (100%)** in ~15 seconds.*

### 3. Run the Evaluation Benchmark Runner
Executes the comprehensive scorecard across all 11 ground truth benchmark scenarios defined in [`eval/ground_truth.json`](eval/ground_truth.json):
```powershell
python -m eval.run_benchmark
```
*Current Verified Benchmark Result:*
```
==========================================================================================
  AUTONOMOUS AI TASK WORKER - COMPREHENSIVE BENCHMARK RUNNER
==========================================================================================
Scenario ID                      Category   Status   Time (s)   Key Outcome
------------------------------------------------------------------------------------------
A1_contract_expiry               A          [PASS]   1.24       Answer contained expiry: True; DB intact: True
B1_normal_invoice                B          [PASS]   3.36       WE-2026-0412 recorded: True; Total records: 4
C1_superseded_invoice            C          [PASS]   10.94      Recorded R1 revision: True; Excluded obsolete original: True
D1_duplicate_invoice             D          [PASS]   8.27       Duplicate prevented: True; Reported existing INV-1002: True
D2_missing_invoice               D          [PASS]   9.37       Honest reporting (nothing invented): True; DB unchanged: True
D3_malformed_amount              D          [PASS]   9.15       Status=ESCALATED: True; DB unchanged: True
D4_ambiguous_candidates          D          [PASS]   9.20       Halted for user clarification: True; Resumed & recorded specified invoice: True
D5_missing_due_date              D          [PASS]   10.40      Derived Net 45 due date 2026-10-20: True; Disclosed derivation: True
D6_timeout_after_commit          D          [PASS]   9.55       504 handled via reconciliation: True; Zero duplicates: True
D7_transient_error_before_commit D          [PASS]   8.93       503 bounded retry succeeded: True
E1_payment_needs_approval        E          [PASS]   18.16      Gate enforced before write: True; Denied safely: True; Approved & Paid: True
------------------------------------------------------------------------------------------
TOTAL: 11/11 PASSED (100.0%)

EVALUATION CRITERIA SCORING:
  * Autonomy       : 100% - Formulates sub-goals, chooses tools, and acts without manual step-by-step guidance.
  * Execution      : 100% - Directly reads real documents, calls HTTP APIs, writes to SQLite, verifies records.
  * Reliability    : 100% - Handles 503 before-commit retries, 504 after-commit reconciliation, and malformed inputs.
  * Verification   : 100% - Independent read-after-write verification on SQLite state; fail-closed on evidence mismatch.
  * Generalization : 100% - Extensible format-adapter architecture (PDF, OCR, DOCX, XLSX, CSV, images).
==========================================================================================
```

---

## 11. Security & Reliability Considerations

### Approval Gate Implementation Details
- **Token Cryptography**: Approval tokens are HMAC-SHA256 signatures generated over a normalized payload:
  `{"record_id": ..., "amount": ..., "currency": ..., "scope": "payment_submit", "nonce": ..., "exp": ...}`.
- **Transaction Binding**: An approval token issued for invoice `INV-1002` cannot be applied to `INV-1003`, nor can the approved amount be altered.
- **Expiration & Replay Prevention**: Tokens carry an explicit UNIX timestamp expiration (15 minutes). Once consumed by `POST /payments`, the unique nonce is recorded in SQLite table `consumed_approval_tokens`; subsequent submissions of the same token return `403 Forbidden`.
- **Role Verification**: High-value payments ($\ge \text{INR } 50,000$) explicitly check `approver_role == "finance_manager"`. Attempts by supervisors or unauthorized roles fail closed.

### Security Limitations & Boundaries
- **Simulated Secret Storage**: The shared HMAC secret is loaded via environment variable (`COMPANY_ENV_PAYMENT_SECRET`). In an enterprise production deployment, token signing would be handled by an independent Identity Provider (IdP) or Hardware Security Module (HSM).
- **In-Process Role Assertion**: In the local demonstration UI, the approver role is selected from a dropdown. A production deployment requires OAuth2 / OIDC authentication tokens with cryptographically signed role claims.

---

## 12. Known Limitations

1. **OCR Coverage**: Raster image OCR relies on a lightweight heuristic parser and Python `Pillow` fallbacks; noisy, low-resolution scanned documents without clear text headers may require an integrated OCR engine (such as Tesseract or cloud document vision APIs).
2. **Table Extraction**: Multi-page tables with split rows in PDFs are flattened into text streams rather than fully structured relational schemas.
3. **Single Process Locking**: The local SQLite database uses WAL mode with short busy timeouts; high-concurrency write stress may experience `sqlite3.OperationalError: database is locked`.
4. **Synchronous LLM Client**: The Groq SDK client call is executed synchronously in worker threads; under high interactive load, an async HTTP client (`httpx.AsyncClient`) would provide higher throughput.

---

## 13. Assumptions

1. **Controlled Document Workspace**: Synthetic enterprise documents are stored within a local directory (`data/documents/`), protected by strict path-containment assertions.
2. **Deterministic AP Rules**: Invoices follow standard accounting patterns (explicit total amounts, vendor names, and Net payment terms).
3. **Simulated Company State**: The enterprise environment runs as a standalone FastAPI instance with SQLite persistence to allow safe, deterministic fault injection.

---

## 14. What I Would Build Next

- [ ] **OIDC / SSO Integration**: Replace simulated role headers with genuine OAuth2 / OpenID Connect JWT verification for human approvals.
- [ ] **Dual-Control (Maker-Checker) Workflows**: Implement multi-signature approval rules requiring two distinct authorized finance managers for payments exceeding INR 100,000.
- [ ] **Advanced Vision OCR**: Integrate native Vision Language Models (e.g. Gemini Flash Vision) for challenging scanned receipts and distorted handwriting.
- [ ] **Asynchronous Task Queue**: Transition orchestrator tasks to a background Celery / Redis queue with WebSocket streaming for live step-by-step UI updates.
- [ ] **Distributed Audit Journal**: Persist execution traces to an immutable, append-only audit log (e.g. OpenTelemetry trace store or Kafka).

---

## 15. Demonstration Video

> **Demo Video Link**: `[Insert Demonstration Video URL Here]`

### Recommended Demo Workflows:
1. **Interactive Document Discovery**: Uploading and inspecting multi-format invoices in the dashboard.
2. **Autonomous Record Creation**: Submitting `"Record the latest Wayne Enterprises invoice"` and watching live SQLite updates in the activity feed.
3. **Fault Tolerance**: Triggering a 504 Gateway Timeout and demonstrating automatic reconciliation without duplicate invoices.
4. **Approval Gate Enforcement**: Attempting an unauthorized direct payment (blocked with 403), followed by an approved payment execution with role validation.
