# ESCO Engineering City

Local-first engineering review and delivery software for ESCO Group. The homepage is a large, interactive Three.js engineering city: drag to orbit 360°, scroll or pinch to zoom, inspect workflow districts, or enter optional first-person Walk mode. The city, Engineering Workspace, evidence, approvals, reports, and Release Center all derive from one project state.

## What is implemented

- Engineering City — 23 named operational zones, dense skyline and infrastructure, canals, bridges, utilities, renewables, logistics, moving traffic and pedestrians, semantic district focus, event replay, quality controls, free orbit/zoom, and bounded Walk mode
- Engineering Workspace — intake, files, tasks, Project Core requirements/devices/I-O/tags/PLC, planning, change sets, revisions, findings, exact evidence, and explanation-gated approvals
- Delivery and reporting — five read-only cross-discipline audit plugins plus a 19-report catalog and real HTML, PDF, DOCX, XLSX, CSV, and JSON generation behind the backend-owned execution boundary
- Release Center — 13 conservative readiness checks, the exact `00 Manifest` through `06 Audit` package tree, provenance, validation/import-verification truth, and API-backed downloads only
- Desktop — Windows-first Tauri 2 shell with loopback-only services, canonical authorized paths, a safe artifact allowlist, approval/failure notifications, support logs, and no general shell/opener permission

Live REST, authentication, contract-checked project data, event replay, and WebSocket updates are integrated against contract `1.0.0`. Canonical Project Core mutation and release/generation/ZIP operations that are not in the contract remain disabled with an exact reason; the UI never substitutes demo success.

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

## Validate the product

```powershell
.\scripts\product\test.ps1
.\scripts\product\test.ps1 -IncludeE2E
pwsh -NoProfile -File .\scripts\product\tests\common.tests.ps1
.venv\Scripts\python.exe -m pytest backend\tests_delivery -q
cargo test --locked --manifest-path desktop\src-tauri\Cargo.toml
cargo clippy --locked --all-targets --manifest-path desktop\src-tauri\Cargo.toml -- -D warnings
```

Rust is required for desktop checks and builds. Tauri CLI 2 is required only to create MSI/NSIS installers; it is intentionally not installed globally by setup.

## Safety boundary

- No telemetry, silent cloud fallback, or automatic external-provider connection
- No secrets in frontend bundles
- No PLC/controller upload, download, online edit, mode change, force, or live equipment control
- AI output remains advisory; restricted and release decisions require an authorized human record
- Desktop URLs are structurally validated loopback URLs; redirects are not followed outside that boundary
- Project and artifact paths are canonicalized, limited to configured local roots, and reject UNC/device namespaces
- Report downloads must remain on the configured API host

Supported parsers and providers are backend-reported. FactoryTalk runtime validation, physical-behavior simulation, `.ACD` parsing, and automatic engineering approval are unsupported.

See the [product guide](docs/product/PRODUCT.md), [Windows and desktop guide](docs/product/DESKTOP.md), and [current Sol handoff](docs/handoffs/SOL.md).
