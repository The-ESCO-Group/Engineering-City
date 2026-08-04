# ESCO Engineering City

Local-first engineering review and delivery software for ESCO Group. The homepage is a large, interactive Three.js engineering city: orbit 360°, zoom from the skyline to individual districts, inspect named agents as they move through project work, or enter optional first-person Walk mode. The city, Engineering Workspace, evidence, approvals, reports, and Release Center all derive from one project state.

## What is implemented

- Engineering City — 23 named operational zones, dense skyline and infrastructure, canals, bridges, utilities, renewables, logistics, moving traffic and pedestrians, semantic district focus, event replay, quality controls, free orbit/zoom, and bounded Walk mode
- Quick project launch — one screen asks only for the project name, controller target, source files, and engineering-responsibility acknowledgement. Customer/site/objective are optional, while standards, HMI, release, and restricted-domain overrides remain available under Advanced settings
- Source intake and Project Core — bounded ZIP expansion plus L5X, CSV, XLSX, DOCX, PDF, XML, and text parsing; exact evidence anchors; requirements, devices, I/O, tags, and PLC records; and a conservative canonical import that copies explicit L5X/I/O-list facts without overwriting human, generated, or approved work
- Autonomous workflow — a durable, resumable task graph inventories and parses files, builds evidence, extracts requirements, plans specialist work, designs tags and PLC structure, expands approved logic patterns, runs delivery agents and cross-discipline validation, and prepares a release candidate. Multiple workers have stable callsigns, visible city locations, task assignments, and honest waiting/blocked states. Agents create questions instead of filling unknown engineering facts with guesses
- Human control points — a Clarification Inbox, accept/reject decisions for extracted requirements, initial and final senior review, six named approval gates, finding decisions, and a separate named release approval. Settled decisions, comments, identities, and timestamps remain in the audit trail
- Delivery workspaces — searchable HMI, alarm, test/commissioning, and documentation master/detail views, with explicit live-empty states when the canonical backend resource is not contracted
- Operations — searchable agent status/location/task inspection plus a read-only, ordered project-event audit stream with gap and payload detail
- Delivery and reporting — five read-only cross-discipline audit plugins plus a 19-report catalog and real HTML, PDF, DOCX, XLSX, CSV, and JSON generation behind the backend-owned execution boundary
- Release Center — persisted readiness, candidates, export artifacts, frozen manifests, and immutable `RC-####.zip` archives. The deterministic package contains `00_Manifest`, `01_Controller`, `02_HMI`, `03_Alarms`, `04_Testing`, `05_Reports`, and `06_Audit`; archive bytes, size, SHA-256, approver, and download path survive process restarts
- Desktop — Windows-first Tauri 2 shell with loopback-only services, canonical authorized paths, a safe artifact allowlist, approval/failure notifications, support logs, and no general shell/opener permission

Live REST, authentication, typed Project Core decisions, persisted Release Center operations, event replay, and WebSocket updates are integrated against contract `1.1.0`. OpenAPI and event/domain schemas in `contracts/` generate checked frontend consumers; the UI refuses a mismatched backend and never substitutes demo success for an unavailable operation.

## Run the synthetic demo

Requirements: PowerShell 7, Node.js 20+, and npm.

```powershell
.\scripts\product\setup.ps1 -SkipBackend
.\scripts\product\run-demo.ps1
```

Open `http://127.0.0.1:4173/?demo=1`. Every synthetic record is labelled `DEMO DATA`. Demo report generation and downloads stay disabled: the catalog and release state are inspectable, but no browser-fabricated file is offered.

## Run the complete local stack

Python 3.12+ is also required.

```powershell
.\scripts\product\setup.ps1
.\scripts\product\dev.ps1
```

The development script starts and supervises the loopback backend first, validates `/api/health`, configures explicit local CORS origins, then starts the frontend. It refuses to reuse an unknown process already occupying either port.

Open `http://127.0.0.1:4173` and sign in with the development-only seeded administrator, `admin@esco.internal` / `esco-demo-admin`. Set `ESCO_BOOTSTRAP_ADMIN_EMAIL`, `ESCO_BOOTSTRAP_ADMIN_PASSWORD`, and `ESCO_BOOTSTRAP_DEMO_USERS=false` for an administered deployment; the defaults are repository-known demo credentials, not production secrets.

To preserve an existing database while starting a separate local workspace, pass an explicit repository-relative or absolute data directory, for example `./scripts/product/dev.ps1 -DataDirectory data/local-live`. The selected directory contains both the SQLite database and project artifacts; it is never reset by the launcher.

## Run an engineering project

1. Open **Project intake**, enter the project name and controller target, add the requirements/project files, acknowledge the engineering boundary, and select **Create project & start agents**.
2. The app returns directly to the city. Follow the named agents, task locations, and live activity before opening **Tasks**, **Clarifications**, or **Project Core** for detail.
3. Answer mandatory questions and explicitly accept or reject extracted requirements. Downstream tasks resume from the database after those decisions.
4. Review each approval request and its evidence, limitations, and validation result. A rejection or unresolved error remains a named block; the workflow does not convert it into success.
5. For a production run, inspect the candidate in **Release Center**. After the candidate manifest and release gate have a current named approval, finalization stores an immutable ZIP under the project's release storage. Download is served only from the authenticated local API and re-verifies the stored archive.

The workflow is autonomous between gates, not unconditionally autonomous. Human engineering authority remains mandatory wherever the system asks a question, accepts a requirement, approves findings or a gate, or accepts the final customer handoff.

## Validate the product

```powershell
.\scripts\product\test.ps1
.\scripts\product\test.ps1 -IncludeE2E
.\scripts\product\test.ps1 -IncludeLiveE2E
pwsh -NoProfile -File .\scripts\product\tests\common.tests.ps1
.venv\Scripts\python.exe -m pytest backend\tests_delivery -q
cargo test --locked --manifest-path desktop\src-tauri\Cargo.toml
cargo clippy --locked --all-targets --manifest-path desktop\src-tauri\Cargo.toml -- -D warnings
```

Rust is required for desktop checks and builds. Tauri CLI 2 is pinned in `desktop/package.json` and installed repository-locally by `setup.ps1 -IncludeDesktop`; no global CLI install is required.

`-IncludeLiveE2E` launches a fresh isolated SQLite backend and separate Vite server, then proves login, typed project creation, ZIP intake, reload persistence, workflow execution, human decisions, stored report bytes, browser download, and completion. It never touches the normal development database. Backend production-workflow and release-container tests separately cover the persisted seven-folder archive path.

## Safety boundary

- No telemetry, silent cloud fallback, or automatic external-provider connection
- No secrets in frontend bundles
- No PLC/controller upload, download, online edit, mode change, force, or live equipment control
- AI output remains advisory; restricted and release decisions require an authorized human record
- Desktop URLs are structurally validated loopback URLs; redirects are not followed outside that boundary
- Project and artifact paths are canonicalized, limited to configured local roots, and reject UNC/device namespaces
- Report downloads must remain on the configured API host

Supported parsers, report providers, and workstation-adapter status are backend-reported. `.ACD` parsing, OCR of scanned PDFs, physical-behavior simulation, controller communications, and automatic engineering approval are unsupported. L5X and declared I/O CSV facts have a canonical importer; other vendor project formats remain evidence until a dedicated, tested importer exists. FactoryTalk/Studio 5000 import verification requires a separately configured licensed workstation and named authorization, so the normal offline result is an explicitly labelled import-ready candidate, not an import-verified controller project.

See the [product guide](docs/product/PRODUCT.md), [Windows and desktop guide](docs/product/DESKTOP.md), and [current Sol handoff](docs/handoffs/SOL.md).
