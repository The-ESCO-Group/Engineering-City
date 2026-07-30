# GPT product handoff

## Status

Work began against bootstrap commit `2f47487`. Backend foundation commit `08c3cfb` subsequently published contract version `1.0.0`; Claude-owned Milestone 1 work is currently active in the shared worktree.

## Completed

- Strict React/Vite foundation with Router, Query, Zustand, Vitest, and Playwright.
- Immersive full-viewport Three.js WebGL operations city with a central landmark skyline, eight workflow districts, hundreds of deterministic instanced buildings, connected metropolitan blocks, a stable isometric overview, first-person walking with pointer-lock mouse look, semantic agent mapping, raycast selection, and reduced motion.
- Reference-aligned command-center chrome with translucent labeled navigation, always-visible cyan district metrics, a functional minimap control stack, selectable six-layer dock, system status, and lower-right city overview.
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
- Lint: `npm --prefix frontend run lint`
- E2E: `npm --prefix frontend run test:e2e`
- Desktop: `.\scripts\product\build-desktop.ps1`

## Known limitations

- The city is a procedural Three.js scene built from real meshes, instancing, materials, lights, and shadows. Its digital-twin palette combines continuous deep-navy urban terrain, cyan infrastructure and water, orange mobility paths, purple district accents, and a vivid green park. The former global grid and visible platform boundary remain removed. The supplied references guide composition and visual language; the implementation retains ESCO branding and uses no external runtime artwork.
- Desktop backend process coordination awaits a documented backend executable command.
- Backend report formats await provider interfaces; demo downloads are real JSON artifacts.

## Commits

- `d5560d39f654ae136692f1478efbab29a9d1db49` — verified product demo slice, desktop shell, scripts, tests, and documentation.
- `46f8fba4f3005826030432ec01ad2d936410f22b` — realistic PixiJS industrial campus, startup diagnostics, verified Tauri render, and Windows resources.
- `5034f1820d5fa66bf4f09138ba5ec6e685361002` — replaced the 2.5D renderer with a true Three.js WebGL campus on a black world and layered white grid.
- `97ae786ecd115d4ca85587787ddd7c15041d91a9` — expanded the renderer into a dense, reference-driven multi-district operations city with instanced geometry, infrastructure, and an accessible map HUD.
- `21e562809f9cd117ee0f0769a86114f547dc1cda` — rebuilt the home as a colorful full-viewport digital twin with emissive districts, polished façade detail, functional minimap/layer chrome, and a no-grid developed world.
- `d69072c9a2e301a2d31d7278e9f82748e74fcbea` — expanded the city into a connected edge-to-edge metro and added first-person walking, pointer-lock mouse look, and verified map/walk controls.
