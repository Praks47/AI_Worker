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


