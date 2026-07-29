# Contracts

The published interface between the ESCO Engineering City backend and every
client (web frontend, desktop shell, reporting plugins).

**These files are generated. Do not edit them by hand.**

```bash
python scripts/backend/generate_contracts.py           # regenerate
python scripts/backend/generate_contracts.py --check   # fail if stale
```

`backend/tests/test_contracts.py` runs `--check`, so an API change that is not
reflected in the committed contracts fails the test suite.

## Files

| File | What it describes | Generated from |
| --- | --- | --- |
| `openapi.yaml` | Every HTTP endpoint, request and response | the live FastAPI app |
| `events.schema.json` | The WebSocket event envelope and per-event payloads | `app.api.schemas.events` |
| `domain.schema.json` | Domain enums and entity shapes | `app.api.schemas.*`, `app.core.enums` |

## Contract version

The current version is recorded in three places that are kept in step:

* `app.CONTRACT_VERSION` (the source of truth)
* `info.x-contract-version` in `openapi.yaml`
* `x-contract-version` in both JSON Schema files

`GET /api/health` returns it, so a client can detect a mismatch at startup.

Versioning follows semver:

* **patch** — documentation only,
* **minor** — additive change (new endpoint, new optional field, new event type),
* **major** — breaking change (removed or retyped field, removed endpoint).

Breaking changes are not made casually; the frontend generates its client from
these files.

## Events

Every event is written to the `events` table **before** it is broadcast. Each
carries a per-project, gap-free `seq`, so a client that reconnects passes the
last sequence number it saw:

```
GET  /api/projects/{project_id}/events?since_seq=41
WS   /ws/projects/{project_id}?since_seq=41
```

The WebSocket answers with a `hello` frame giving `current_seq` and how many
events were replayed. If the requested `seq` is older than the replay window,
`replay_truncated` is `true` and the client must refetch state over REST rather
than assume it has a complete picture.

Switch on `event_type`, then read `payload`. `x-payload-by-event-type` in
`events.schema.json` maps each event type to its payload definition.

## Errors

Every failing endpoint returns the same envelope:

```json
{ "error": { "code": "file_rejected", "message": "…", "details": {} } }
```

`code` is stable and safe to branch on; `message` is for humans and may change.

## Generating a client

```bash
npx openapi-typescript contracts/openapi.yaml -o src/api/schema.d.ts
npx json-schema-to-typescript contracts/events.schema.json -o src/api/events.d.ts
```

## What this API deliberately does not do

These are product constraints, not gaps to be filled later. There is no endpoint
for any of them, and there will not be one:

* connecting to an industrial controller, or reading/writing controller memory;
* uploading or downloading a controller program;
* modifying a running PLC;
* parsing proprietary `.ACD` files;
* applying a PLC or electrical design change;
* releasing a safety- or equipment-damage-related recommendation without a
  recorded human approval;
* returning raw model reasoning.

Agent output is a **proposal with evidence**, never approved engineering.
