# Product guide

ESCO Engineering City is a local engineering-operations workspace. Its multi-district city explains workflow state; findings and source evidence remain the center of the experience.

## Principles

- Agent positions derive from semantic workflow state, never random movement.
- The home is an immersive Three.js WebGL digital-twin world with a central skyline, eight workflow districts, hundreds of deterministic instanced buildings, roads, bridges, waterways, industrial and logistics infrastructure, and layered system overlays.
- Its visual language uses a deep-navy environment, luminous cyan infrastructure and water, orange mobility routes, purple district accents, and a vivid green park. Developed terrain stays free of a global black grid so streets, districts, and building footprints provide the spatial structure.
- The viewport chrome mirrors an operations command center: translucent labeled navigation, city callouts, a lower-left minimap, a bottom layer dock, and a live overview panel sit over the world instead of shrinking it into a dashboard card.
- Demo records are always marked `DEMO DATA`.
- AI recommendations are advisory and never imply approval.
- Critical decisions require an authorized reviewer and recorded rationale.
- No controller upload, download, online edit, or control functions exist.
- Demo mode has no external network dependency.

## Demonstration

1. Run `scripts/product/run-demo.ps1` and open `http://127.0.0.1:4173`.
2. Orbit the 3D city, select district labels or anchor buildings, and inspect deterministic agent placement.
3. Open Findings, select `F-107`, and compare evidence.
4. Open Approvals; submit remains disabled until a decision is selected.
5. Open Reports and generate a synthetic, clearly marked export.

The visualization uses named native controls alongside mouse/touch orbit and raycast selection. System reduced-motion preferences and the explicit reduced-motion option stop animation.
