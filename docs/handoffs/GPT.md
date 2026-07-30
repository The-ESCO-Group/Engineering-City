# GPT product handoff

## Status

Work began against bootstrap commit `2f47487`. Backend foundation commit `08c3cfb` subsequently published contract version `1.0.0`; Claude-owned Milestone 1 work is currently active in the shared worktree.

## Completed

- Strict React/Vite foundation with Router, Query, Zustand, Vitest, and Playwright.
- Original PixiJS isometric industrial campus with eight distinct 3D-style buildings, semantic agent mapping, deterministic occupancy, camera controls, timeline, and reduced motion.
- Project portfolio, task board, findings, evidence comparison, approvals, reports, activity, and workspace tabs.
- Clearly labeled offline demo data and real synthetic JSON browser export.
- Tauri 2 Windows shell with local-only backend URL validation, Windows application resources, and a verified non-blank WebView render.
- Product PowerShell scripts, synthetic fixture boundary, and product docs.

## Contract version

Observed: contract version `1.0.0` in `contracts/openapi.yaml`, `contracts/domain.schema.json`, and `contracts/events.schema.json`. No conflicting handwritten API models were created. Generated-client integration remains pending until the actively changing backend endpoint surface is committed.

## Exact backend change requests

1. Publish versioned OpenAPI and JSON schemas under `contracts/**`, including errors, permissions, `EvidenceAnchor`, reports, and WebSocket envelope.
2. Provide client generation and contract version commands.
3. Define event order, duplicate identity, resume cursor, critical acknowledgement, and REST fallback.
4. Create `ReportProvider` and `ExportFormatProvider` plus artifact storage/download contracts.
5. Create delivery-agent plugin interfaces and normalized HMI/electrical inputs with structured outputs.
6. Document health, authentication, development, demo, and test commands in `docs/handoffs/CLAUDE.md`.
7. Define capabilities for task actions, approvals, reassignment, files, and local directories.

## Blocked owned work

Delivery agents under `backend/app/agents/delivery/**` and report providers under `backend/app/reporting/**` await the Claude-owned base interfaces. Adding speculative implementations would violate the contract boundary. Real login, upload, API, WebSocket, permission, and artifact integration also await contracts.

## Commands

- Setup: `.\scripts\product\setup.ps1`
- Demo: `.\scripts\product\run-demo.ps1`
- Checks: `.\scripts\product\test.ps1`
- E2E: `npm --prefix frontend run test:e2e`
- Desktop: `.\scripts\product\build-desktop.ps1`

## Known limitations

- The campus is rendered by PixiJS as an original procedural 2.5D scene. It does not use copied OpenClaw City artwork or externally hosted runtime assets.
- Desktop backend process coordination awaits a documented backend executable command.
- Backend report formats await provider interfaces; demo downloads are real JSON artifacts.

## Commits

- `d5560d39f654ae136692f1478efbab29a9d1db49` — verified product demo slice, desktop shell, scripts, tests, and documentation.
- `46f8fba4f3005826030432ec01ad2d936410f22b` — realistic PixiJS industrial campus, startup diagnostics, verified Tauri render, and Windows resources.
