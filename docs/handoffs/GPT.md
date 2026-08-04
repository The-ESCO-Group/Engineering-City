# GPT product handoff

## Status

The product surface is integrated with backend contract `1.1.0`. Live intake now creates the typed engineering profile, stages accepted files, and starts a durable run. Clarifications, requirement decisions, approvals, reports, persisted release candidates, immutable release archives, and authenticated downloads are connected end to end. The offline city demonstration remains a deliberately separate synthetic mode.

## Completed

- Strict React/Vite foundation with Router, Query, Zustand, Vitest, and Playwright.
- Immersive full-viewport Three.js WebGL operations city with 23 labeled operational zones, a multi-spire central skyline, dense deterministic instanced buildings, canals, bridges, renewable energy, water treatment, testing, data-center, maintenance, logistics and harbor infrastructure, a broad block-tiled flat-world perspective with free 360° orbit and zoom, first-person walking with pointer-lock mouse look, semantic agent mapping, raycast selection, and reduced motion.
- Deterministic city-life simulation with bidirectional traffic, pedestrian movement and idling, water and route packets, rooftop beacons, and phase-varied window lighting; all ambient motion honors pause, speed, and reduced-motion state.
- City-first spectator chrome closely matched to the expanded ESCO reference, with a compact icon rail, unified command bar, world-tracked spatial labels, functional minimap controls, clearly labeled synthetic activity strip, compact six-layer dock, and operational summary.
- Project portfolio, task board, findings, evidence comparison, approvals, reports, activity, and workspace tabs.
- Checkpointed `engineering_report_delivery` plugin that snapshots only human-accepted findings, generates discrepancy-matrix and test-plan artifacts, stores traceable `Report.finding_ids`, emits `report.created`, and resumes without duplicating completed provider/format pairs.
- Built-in discrepancy-matrix and test-plan providers with Markdown and HTML export formats. Reports keep facts separate from inferences, carry limitations and confidence, and reproduce evidence `display_location` verbatim.
- Typed local REST/WebSocket frontend integration for project identity, agent presence, activity messages, findings and citation detail, approvals and decisions, report downloads, parser capabilities, and provider status. The UI reconnects with `since_seq`, invalidates from live events, refetches after truncated replay, and retains an explicit synthetic fallback when no authenticated backend project is available.
- Typed five-step live intake for project identity, exact controller/HMI target, release mode and restricted domains, pinned engineering standards/templates, source files, and engineering-responsibility confirmation. Project creation sends the profile as a first-class contract object rather than opaque attributes.
- Clarification Inbox for project-level questions and object/requirement ambiguities, including named answers and automatic refresh after the backend unblocks affected work.
- Canonical requirements resource in Project Core with explicit accept/reject decisions and rationale; proposed requirements are not presented as approved engineering truth.
- Persisted Release Center integration for readiness, candidate creation, deterministic artifact generation, current candidate/artifact reload, named finalization, immutable archive metadata, and authenticated `RC-####.zip` download. The adapter prefers persisted resources so a backend restart or empty event replay does not erase release state.
- Contract-driven feature gating and WebSocket invalidation for clarification, requirement, export, validation, release, and approval changes. Unsupported actions remain disabled with the backend's reason.
- Clearly labeled offline demo data and real synthetic JSON browser export.
- Tauri 2 Windows shell with local-only backend URL validation, Windows application resources, and a verified non-blank WebView render.
- Product PowerShell scripts, synthetic fixture boundary, and product docs.

## Contract version

Current: contract version `1.1.0` in `contracts/openapi.yaml`, `contracts/domain.schema.json`, and `contracts/events.schema.json`. `frontend/scripts/generate-contracts.mjs` is pinned to the same version and generates the frontend OpenAPI, domain, event, and version consumers. `npm --prefix frontend run contracts:check` fails on drift or a version mismatch.

## Integrated autonomous surface

- Engineering profile REST resources expose completeness, target-platform truth, restrictions, standards/templates, and generation eligibility.
- File intake safely expands ZIPs and reports per-member acceptance. Installed parsers publish exact evidence plus explicit warnings/unsupported items for L5X, CSV, XLSX, DOCX, PDF, XML, and text.
- The production task graph persists inventory, parsing, evidence and canonical import, requirements/planning, clarification, design/generation, validation, senior review, approval, delivery, import-verification, package, and finalization stages.
- Project questions, clarifications, requirements, findings, and six approval gates retain named human decisions. Agents block or ask instead of manufacturing missing engineering authority.
- Release readiness, candidates, export artifacts, manifests, and archives are backend resources. Finalization re-verifies the approved package and stores deterministic archive bytes, size, SHA-256, approver identity, and download path independently of event retention.
- Reports use the complete 19-entry catalog and real local HTML, PDF, DOCX, XLSX, CSV, JSON, and Markdown format providers.

## Cross-owner integration

The frontend consumes `contracts/**` read-only and keeps browser state subordinate to persisted backend resources. Product-owned backend integration remains limited to delivery agents/reporting and the corresponding delivery tests; backend domain and API ownership stays documented in the repository layout.

The live browser acceptance path creates a typed project, uploads a synthetic ZIP, observes persisted tasks after reload, records at least one decision through the UI, settles remaining synthetic approvals through the authenticated API, verifies stored report bytes, downloads a report through the browser, and waits for completion. Production release-container behavior is covered separately by backend orchestration, API, persistence, and restart tests.

## Commands

- Setup: `.\scripts\product\setup.ps1`
- Demo: `.\scripts\product\run-demo.ps1`
- Checks: `.\scripts\product\test.ps1`
- Lint: `npm --prefix frontend run lint`
- E2E: `npm --prefix frontend run test:e2e`
- Desktop: `.\scripts\product\build-desktop.ps1`

## Known limitations

- The city is a procedural Three.js scene built from real meshes, instancing, materials, lights, and shadows. Its daylight digital-twin palette combines a fog-blended block-tiled flat world, cyan infrastructure and water, orange mobility paths, purple district accents, and varied green public space. The former global black grid and visible platform boundary remain removed. City activity is deterministic synthetic demo motion, not a claim of live backend telemetry. The supplied references guide composition and visual language; the implementation retains ESCO branding and uses no external runtime artwork.
- The packaged Tauri shell still does not bundle or supervise Python; a workstation installation needs a separately administered contract-`1.1.0` loopback backend. The product development script supervises both services locally.
- The canonical source importer currently promotes explicit current L5X facts and declared I/O-list CSV facts. Other parsed documents contribute evidence and requirements but do not become a universal vendor-project conversion. `.ACD` parsing, OCR, and arbitrary proprietary project import are not implemented.
- The normal local workflow produces an explicitly labelled offline import-ready candidate. Studio 5000/FactoryTalk import verification requires a configured licensed-workstation adapter and a named authorized job; the unattended workflow does not run that adapter by itself.
- Restricted safety, motion, burner-management, process-safety, high-energy, electrical-power-distribution, and robotics work is blocked or manual-review-only unless a qualified workflow is deliberately configured. No PLC communication or live-control capability exists.
- Autonomous work pauses for mandatory clarifications, requirement decisions, findings, approval gates, and final release acceptance. It does not replace the engineer of record, and source quality, registered templates, target-profile completeness, and human review determine deliverable quality.
- Disconnected demo generation/download remains intentionally disabled. Real reports and release archives exist only in an authenticated live-local project.

## Commits

- `d5560d39f654ae136692f1478efbab29a9d1db49` — verified product demo slice, desktop shell, scripts, tests, and documentation.
- `46f8fba4f3005826030432ec01ad2d936410f22b` — realistic PixiJS industrial campus, startup diagnostics, verified Tauri render, and Windows resources.
- `5034f1820d5fa66bf4f09138ba5ec6e685361002` — replaced the 2.5D renderer with a true Three.js WebGL campus on a black world and layered white grid.
- `97ae786ecd115d4ca85587787ddd7c15041d91a9` — expanded the renderer into a dense, reference-driven multi-district operations city with instanced geometry, infrastructure, and an accessible map HUD.
- `21e562809f9cd117ee0f0769a86114f547dc1cda` — rebuilt the home as a colorful full-viewport digital twin with emissive districts, polished façade detail, functional minimap/layer chrome, and a no-grid developed world.
- `d69072c9a2e301a2d31d7278e9f82748e74fcbea` — expanded the city into a connected edge-to-edge metro and added first-person walking, pointer-lock mouse look, and verified map/walk controls.
- `a5b31b83076e77131ca3b20c71093370165ea40d` — brought the city to life with moving traffic and pedestrians, animated infrastructure, street-level detail, cleaner spectator chrome, and updated visual regression previews.
- `52e67f18acfcb0cf43229441638000e8c5e6786a` — preserved the composed city homepage while adding unrestricted horizontal Map orbit, wheel/pinch zoom, grab affordances, reset behavior, and end-to-end interaction coverage.
- `6f052f5dd8604283a93a13b14d8b0f1a5ab9d88b` — replaced the overview diorama projection with a broad perspective flat world, fog-blended voxel terrain chunks, retuned spatial labels, and updated overview/walk previews.
- `6f848a7d44d66eed8146aca1f00df7f9cecd043a` — moved the opening overview about one-third closer so individual buildings read at metropolitan scale while preserving the flat-world horizon, 360° orbit, zoom, reset, and Walk mode.
