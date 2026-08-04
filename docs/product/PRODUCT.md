# Product guide

ESCO Engineering City is a local engineering-operations workspace. Its multi-district city explains workflow state; findings and source evidence remain the center of the experience.

![Large-scale engineering city overview](product-preview.png)

![First-person street view](first-person-preview.png)

## Principles

- Agent positions derive from semantic workflow state, never random movement.
- The home is an immersive Three.js WebGL digital-twin world with 23 labeled operational zones, eight interactive workflow districts, a multi-spire central skyline, dense deterministic instanced buildings, connected metropolitan blocks, roads, bridges, visible canals, renewable-energy sites, water treatment, testing, data-center, logistics, maintenance, and harbor infrastructure.
- The city is visibly active: deterministic demo traffic moves in both directions, pedestrians circulate through the plaza, park, and engineering forecourts, water and workflow routes pulse, and building beacons/windows change over time. Pause and reduced-motion freeze this ambient activity.
- The overview opens as the large reference composition: developed land reaches both sides, distant districts establish depth, the skyline, roundabout, and park form a readable center, and foreground infrastructure continues beneath the HUD. Free 360° drag orbit plus wheel or pinch zoom works without first-person mode; spatial labels remain attached to their real districts. Reset restores the opening composition, while a separate first-person mode provides street-level exploration.
- Its visual language uses a daylight-blue environment, continuous block-tiled terrain, luminous cyan infrastructure and water, orange mobility routes, purple district accents, and varied green public space. The subtle chunk variation gives the surrounding world a voxel-built scale without reintroducing a global black grid or visible platform edges.
- The city-first chrome is one coherent spectator interface: a compact navigation rail, unified command bar, lightweight spatial labels, a small minimap, a clearly marked synthetic activity strip, compact layers, and an operational summary frame the world without shrinking it into a dashboard card.
- Demo records are always marked `DEMO DATA`.
- AI recommendations are advisory and never imply approval.
- Critical decisions require an authorized reviewer and recorded rationale.
- No controller upload, download, online edit, or control functions exist.
- Demo mode has no external network dependency.

## Demonstration

1. Run `scripts/product/run-demo.ps1` and open `http://127.0.0.1:4173`.
2. Use **Map** to inspect the connected city. Drag anywhere on the world to orbit 360°, use the wheel or a pinch gesture to zoom, and choose **Reset city view** to restore the opening composition.
3. Watch the synthetic traffic, pedestrians, water packets, workflow routes, and activity strip. Pause the timeline to freeze all ambient city motion.
4. Choose **Walk**, click the world to enable mouse look, move with WASD or the arrow keys, and hold Shift to move faster. Press Escape to release the cursor and **Exit walk** to return to the map.
5. Open Findings, select `DEMO-F-107`, and compare the requirement and implementation evidence at their exact cited locations.
6. Open Approvals. Submission requires a supported decision and, where policy requires it, a recorded engineering comment; the named reviewer remains accountable.
7. Open Reports and Release Center to inspect the 19-entry catalog, conservative readiness, and exact package tree. Demo generation and download stay disabled with an explicit reason.

The visualization uses named native controls alongside map zoom, raycast selection, and first-person navigation. System reduced-motion preferences and the explicit reduced-motion option stop animation.

Windows setup, local-service supervision, filesystem authorization, support logs, and desktop packaging are documented in [Local desktop and Windows operations](DESKTOP.md).

## Engineering Workspace

The workspace is one traceable review surface rather than disconnected PLC, HMI, and alarm applications. Live intake creates the project and its canonical engineering profile in one transaction. The profile records the exact controller/HMI target, firmware and Studio 5000 versions, pinned standards and templates, release mode, safety classification, and restricted domains; an incomplete or unsupported profile blocks generation visibly.

Authorized source files and safe ZIP members become backend-owned records. The installed parsers cover L5X, CSV, XLSX, DOCX, PDF, XML, and text and publish exact evidence anchors plus warnings and unsupported-content records. The evidence-building stage imports only explicit, current L5X and declared I/O-list CSV facts into Project Core. It does not infer missing addresses or types, and it does not overwrite human-authored, generated, approved, restricted, or otherwise occupied canonical records.

The resulting workspace includes files and backend tasks; a Clarification Inbox; requirements, devices and I/O, tags, and PLC records; plans, structured change proposals, and revisions; findings with fact/inference/limitations separated; exact evidence citations; and explanation-gated human approvals. Extracted requirements remain proposed until a named user accepts or rejects them. HMI and alarm concerns appear through cross-discipline findings, evidence, change impact, delivery artifacts, reports, and release gates. Unsupported operations remain visible but unavailable.

## Autonomous production workflow

A production run is a persisted dependency graph, not a browser animation. The local worker leases tasks, checkpoints completed work, resumes after restart, and requeues an expired lease without duplicating a completed stage. In order, it:

1. inventories and parses accepted files, builds the evidence graph, and imports eligible canonical facts;
2. extracts requirements and plans supported specialist reviews in parallel;
3. stops for mandatory questions and generation-blocking clarifications;
4. performs initial senior review, then waits at the project-profile approval gate;
5. designs and validates tags/I/O, waits at the devices/I/O/tags gate, then designs PLC structure and expands approved logic patterns;
6. validates logic and delivery, waits at that gate, runs installed delivery/report agents, validates across disciplines, and performs final senior review;
7. waits at the QA/risk gate and all outstanding finding approvals;
8. generates a release candidate, records licensed-workstation import verification as passed, failed, unavailable, or not attempted without conflating those states, and waits at the import-verification gate;
9. asks a named engineer to approve and freeze the manifest, waits at the release gate, then finalizes the immutable delivery archive.

Tasks behind an unresolved gate remain queued. Clarification and approval decisions unblock the affected tasks from persisted state; users do not have to restart the project. Review-only runs stop after review/report delivery and do not pretend to produce a controller release.

## Reports and Release Center

The Release Center reads persisted readiness, candidate, artifact, manifest, and archive resources first, with events used for live invalidation and audit context. It conservatively presents 13 readiness checks, keeps all 19 required report types visible, and shows real HTML, PDF, DOCX, XLSX, CSV, and JSON outputs supplied by the backend. It renders the exact `00_Manifest`, `01_Controller`, `02_HMI`, `03_Alarms`, `04_Testing`, `05_Reports`, and `06_Audit` tree, including explicit absence notes when an optional delivery discipline produced nothing.

Creating a candidate, generating artifacts, approving its manifest, finalizing the archive, and downloading the archive are distinct authenticated operations. Finalization verifies the frozen manifest, checksums, approval identity, package path, and expected files before storing a deterministic `RC-####.zip` plus its byte count and SHA-256. Those database and filesystem records survive backend and browser restarts; download streams the stored bytes only after verification. A generated candidate does not imply Studio 5000 import verification, human approval, or release, and the UI keeps those states separate.

## Current capability boundary

The autonomous pipeline can turn supported, explicit source facts and approved templates into a traceable review, reports, and an offline import-ready Rockwell package. It is not a universal vendor-project translator. `.ACD` files, OCR-only/scanned PDF text, unsupported XML/vendor structures, ambiguous I/O sheets, and unqualified restricted-domain work are reported or blocked rather than guessed. Quality therefore depends on parseable source material, a complete target profile, registered standards/templates, and the required human decisions.

The default local installation has no controller connection and no licensed Rockwell workstation. Its highest offline technical verification is deterministic generation plus internal semantic checks and L5X round-trip parsing. Actual Studio 5000/FactoryTalk import verification requires a separately configured licensed-workstation adapter and named approval for that job. Human reviewers remain accountable for requirements, findings, approval gates, risk acceptance, and final delivery.

## Desktop integration

Settings shows the browser/native runtime boundary. In the installed Tauri shell, operators may open only a canonical project directory directly beneath an authorized local root, open an allowlisted artifact inside that project, locate native support logs, and receive bounded OS notifications for pending human approvals or workflow failures. The browser shows these controls disabled with the reason. The shell has no unrestricted path opener, command shell, or live-equipment capability.
