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

The workspace is one traceable review surface rather than disconnected PLC, HMI, and alarm applications. It includes guided intake; authorized files and backend tasks; requirements, devices and I/O, tags, and PLC records; plans, structured change proposals, and revisions; findings with fact/inference/limitations separated; exact evidence citations; and explanation-gated human approvals. HMI and alarm concerns appear through cross-discipline findings, evidence, change impact, reports, and release gates. Unsupported backend operations remain visible but unavailable.

## Reports and Release Center

The Release Center conservatively derives 13 readiness checks from API and event facts. It renders the exact seven-folder package structure from `00 Manifest` through `06 Audit`, keeps all 19 required report types visible, and shows HTML, PDF, DOCX, XLSX, CSV, and JSON availability. A download exists only when the authorized API supplied a generated file URL on its own host. Candidate generation does not imply import verification, approval, or release; those states remain separate.

## Desktop integration

Settings shows the browser/native runtime boundary. In the installed Tauri shell, operators may open only a canonical project directory directly beneath an authorized local root, open an allowlisted artifact inside that project, locate native support logs, and receive bounded OS notifications for pending human approvals or workflow failures. The browser shows these controls disabled with the reason. The shell has no unrestricted path opener, command shell, or live-equipment capability.
