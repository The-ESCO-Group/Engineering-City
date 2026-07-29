# GPT product handoff

## Status

Work began against bootstrap commit `2f47487`. No `contracts/**`, backend app, or `docs/handoffs/CLAUDE.md` existed.

## Completed

- Strict React/Vite foundation with Router, Query, Zustand, Vitest, and Playwright.
- Original isometric campus with eight stable building IDs, semantic agent mapping, timeline, and reduced motion.
- Project portfolio, task board, findings, evidence comparison, approvals, reports, activity, and workspace tabs.
- Clearly labeled offline demo data and real synthetic JSON browser export.
- Tauri 2 Windows shell with local-only backend URL validation.
- Product PowerShell scripts, synthetic fixture boundary, and product docs.

## Contract version

Blocked: `contracts/**` does not exist. No conflicting client API models were created.

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

- The campus is currently DOM/CSS-rendered; PixiJS is installed for migration once event schemas arrive.
- Desktop backend process coordination awaits a documented backend executable command.
- Backend report formats await provider interfaces; demo downloads are real JSON artifacts.

## Commits

- `d5560d39f654ae136692f1478efbab29a9d1db49` — verified product demo slice, desktop shell, scripts, tests, and documentation.
