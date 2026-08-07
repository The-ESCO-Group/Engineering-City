# ESCO Engineering City

Local-first engineering review and delivery software for ESCO Group. Engineering City turns project intake, autonomous agent work, human review, evidence, reports, and release packaging into one visible 3D operating environment.

> **Engineering City does not control live industrial equipment.** AI output is advisory, evidence must remain traceable, and authorized people retain approval and release authority.

## Product at a glance

| Area | What it provides |
|---|---|
| 3D Engineering City | A large Three.js world with 23 named districts, 360° orbit and zoom, optional Walk mode, moving agents, district focus, and event replay |
| Project intake | A short guided flow for the project name, controller target, source files, and engineering-responsibility acknowledgement |
| Project Core | Evidence-backed requirements, devices, I/O, tags, PLC records, decisions, clarifications, and revisions |
| Autonomous workflow | Durable, resumable agent tasks for intake, evidence, planning, engineering, validation, reporting, and release preparation |
| Human gates | Requirement decisions, findings, clarifications, six approval gates, senior review, validation waivers, and final release authorization |
| Delivery | HMI, alarm, testing, commissioning, documentation, reports, manifests, and deterministic release archives |
| External agents | Project-scoped OpenAI or Claude SDK workers with named city presence, bounded context, short-lived credentials, and evidence-gated submissions |
| Desktop | A Windows-first Tauri 2 application connected only to an explicitly configured loopback backend |

The city is not a decorative layer. Its districts, agent locations, work status, alerts, approvals, and release state are visualizations of the same project records used throughout the Engineering Workspace.

## Architecture

```text
React + Three.js + Tauri
          │
          │ REST, WebSocket, contract 1.1.0
          ▼
FastAPI application
          │
          ├── Project data, evidence, requirements, and decisions
          ├── Durable orchestration and named agents
          ├── Validation, reports, approvals, and audit history
          └── SQLite, project files, artifacts, and release archives
```

OpenAPI, event, and domain schemas live in `contracts/` and generate checked frontend consumers. The UI refuses an incompatible backend and never substitutes demo success for an unavailable live operation.

## Quick start

### Requirements

- PowerShell 7
- Node.js 20+ and npm
- Python 3.12+
- Rust stable only for desktop development

### Run the complete local stack

```powershell
git clone https://github.com/The-ESCO-Group/Engineering-City.git
Set-Location Engineering-City
.\scripts\product\setup.ps1
.\scripts\product\dev.ps1
```

Open `http://127.0.0.1:4173`. The development-only seeded administrator is:

```text
Email:    admin@esco.internal
Password: esco-demo-admin
```

These are repository-known development credentials. For an administered deployment, set `ESCO_BOOTSTRAP_ADMIN_EMAIL`, `ESCO_BOOTSTRAP_ADMIN_PASSWORD`, and `ESCO_BOOTSTRAP_DEMO_USERS=false`.

### Run the synthetic demo

```powershell
.\scripts\product\setup.ps1 -SkipBackend
.\scripts\product\run-demo.ps1
```

Open `http://127.0.0.1:4173/?demo=1`. Synthetic records are labelled `DEMO DATA`; report generation and release downloads remain disabled.

## Project workflow

1. Open **Project intake**, enter a project name and controller target, add the available requirements and engineering files, acknowledge the engineering boundary, and select **Create project & start agents**.
2. Follow named agents in the city or open **Tasks** for detailed status.
3. Review extracted requirements and exact evidence anchors. Resolve mandatory clarifications and explicitly accept or reject requirements.
4. Inspect engineering outputs, findings, change sets, HMI, alarms, tests, documentation, and reports.
5. Review every approval request with its evidence, limitations, and current validation state.
6. In **Release Center**, inspect the candidate manifest, record the required named approval, finalize the archive, and download the verified release ZIP.

The workflow is autonomous between gates, not unconditionally autonomous. Missing engineering facts become questions rather than guesses.

## Bring your own agent

An existing OpenAI or Claude agent can keep its own prompts, tools, memory, handoffs, and orchestration. ESCO adds a thin project boundary around that workflow:

1. Sign in as a project owner or engineer.
2. Open **Agents** and select **Send agent**.
3. Choose the provider, name the city agent, select the model, describe the mission, and acknowledge external processing.
4. Copy the one-time mission token and pass it to the existing worker as `ESCO_AGENT_TOKEN`.
5. The worker reads bounded project context, optionally publishes progress and semantic city locations, then submits one structured result.

The bundled Python adapters demonstrate the contract:

```powershell
python -m pip install openai-agents pydantic
# or
python -m pip install anthropic
```

- [External-agent integration guide](sdk/python/README.md)
- [Provider-neutral ESCO client](sdk/python/esco_city.py)
- [OpenAI Agents SDK worker](sdk/python/openai_worker.py)
- [Claude SDK worker](sdk/python/claude_worker.py)

External agents may create findings and recommendations, post messages, request clarifications or approvals, report blockers, or explicitly report no findings. They cannot approve their own work, waive validation, release a project, or access the database and filesystem directly.

One deployment represents one visible city agent and one mission. A multi-agent workflow can either appear as one orchestrated worker or deploy one scoped mission for each independently visible agent.

## Supported project inputs

| Format | Treatment |
|---|---|
| `.zip` | Bounded expansion followed by supported-file discovery |
| `.l5x` | Parsed evidence and conservative canonical import of explicit controller facts |
| `.csv` | Parsed tabular evidence; declared I/O facts may enter Project Core |
| `.xlsx` | Parsed workbook evidence |
| `.docx` | Parsed document evidence |
| `.pdf` | Extractable text evidence |
| `.xml`, `.txt` | Parsed structured or plain-text evidence |

Unsupported formats remain visible as source evidence and are never silently treated as imported engineering data.

## Desktop application

Prepare and build the Windows application:

```powershell
.\scripts\product\setup.ps1 -IncludeDesktop
.\scripts\product\build-desktop.ps1 -Bundle nsis -BackendUrl http://127.0.0.1:8000
```

The executable and installer are written under `desktop/src-tauri/target/release/`. The desktop shell does not bundle the Python backend; start the local backend before opening a packaged application.

## Release package

Finalized releases use a deterministic structure:

```text
RC-####.zip
├── 00_Manifest/
├── 01_Controller/
├── 02_HMI/
├── 03_Alarms/
├── 04_Testing/
├── 05_Reports/
└── 06_Audit/
```

Archive bytes, size, SHA-256, candidate manifest, approver, provenance, and download path persist across process restarts.

## Validation

```powershell
.\scripts\product\test.ps1
.\scripts\product\test.ps1 -IncludeE2E
.\scripts\product\test.ps1 -IncludeLiveE2E
pwsh -NoProfile -File .\scripts\product\tests\common.tests.ps1
.venv\Scripts\python.exe -m pytest backend\tests_delivery -q
cargo test --locked --manifest-path desktop\src-tauri\Cargo.toml
cargo clippy --locked --all-targets --manifest-path desktop\src-tauri\Cargo.toml -- -D warnings
```

The live end-to-end suite uses isolated frontend, backend, and SQLite instances. It does not touch the normal development database.

## Safety boundaries

- No telemetry or silent cloud fallback
- No secrets in frontend bundles
- No PLC upload, download, online edit, mode change, force, or live equipment command
- No automatic engineering approval or release
- External agents receive project-scoped, short-lived credentials rather than user sessions
- Technical claims require real evidence anchors; invented or inaccessible anchors are rejected
- Project and artifact paths are restricted to configured local roots
- Desktop URLs are structurally validated loopback URLs
- Demo mode cannot fabricate report or release downloads

## Known limitations

- `.ACD` parsing is not supported
- OCR for scanned PDFs is not supported
- Physical-behavior simulation is not a replacement for commissioning or field validation
- Formats without a dedicated, tested importer remain evidence only
- FactoryTalk or Studio 5000 verification requires a separately configured licensed workstation and named authorization
- The normal offline result is **import-ready**, not **import-verified**
- Remotely hosted external agents require a separately secured network gateway or local relay; the standard desktop backend is loopback-only

## Repository layout

```text
Engineering-City/
├── backend/       FastAPI services, orchestration, agents, validation, and reports
├── frontend/      React application, Three.js city, and Engineering Workspace
├── desktop/       Tauri 2 Windows shell
├── contracts/     Versioned API, event, and domain contracts
├── sdk/           External-agent adapters and examples
├── fixtures/      Synthetic project fixtures
├── infra/         Backend and product infrastructure
├── scripts/       Setup, launch, build, smoke, and validation commands
└── docs/          Product, backend, and handoff documentation
```

## Documentation and support

- [Product guide](docs/product/PRODUCT.md)
- [Windows and desktop guide](docs/product/DESKTOP.md)
- [Backend documentation](docs/backend/)
- [Current Sol handoff](docs/handoffs/SOL.md)
- [GitHub Issues](https://github.com/The-ESCO-Group/Engineering-City/issues)

The product launcher prints its support-log location when it starts. Backend health is available at `http://127.0.0.1:8000/api/health`.
