# Security review

The threat model for a local-first engineering review platform is unusual: the
network attacker is largely absent, and the real risks are **untrusted files**,
**untrusted model output**, and **the product asserting something it cannot
support**. The third is the one that gets someone hurt.

## Boundaries the product does not cross

Enforced by there being no code that could do them, not by policy:

* no connection to an industrial controller, and no read or write of controller
  memory;
* no upload or download of a controller program;
* no modification of a running PLC;
* no parsing of proprietary `.ACD` files — the extension is refused at intake
  with a message saying so and pointing at L5X;
* no application of a PLC or electrical design change; a recommendation is a
  proposal for a human and nothing consumes an approved one as an executed
  change;
* no release of a safety- or damage-related recommendation without a recorded
  human decision;
* no outbound network call unless an administrator has explicitly enabled a cloud
  provider;
* no Office automation and no subprocess anywhere in the parsing path.

## Untrusted input

**Intake is an allowlist.** A file is accepted because its type is recognised and
supported, never because it is not on a blocklist. Content is sniffed as well as
extension-checked, because the extension is attacker-controlled.

| Threat | Control |
| --- | --- |
| Executable disguised as a document | Magic-byte check (`MZ`, ELF, Mach-O, shebang, OLE) before anything else |
| Dangerous extension | Refused on sight, whatever the content |
| Oversized upload | Enforced *while copying*, not from a client-supplied `Content-Length` |
| Filename collision | `unique_path` never overwrites |
| Path traversal in a filename | `safe_filename` strips to a single component; `resolve_within` proves containment on the **resolved** path, so a planted symlink cannot redirect a write |
| Zip Slip | Every member name proved containable from the central directory *before* anything is written |
| Zip bomb | Entry count, total uncompressed size and compression ratio checked before decompressing; CRC verification runs last, so a bomb is refused before it costs anything |
| Understated member size | The real byte count is capped during the copy; the declared size is never trusted |
| Symlink / device members | Skipped with a reason recorded |
| XXE, billion laughs | One hardened XML entry point with DTDs and entities disabled; size and depth bounded |
| Decompression bomb in OOXML | The `.docx` package is size- and ratio-checked before python-docx opens it |
| Rendering an upload as active content | `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, and a restrictive CSP on every response |

## Untrusted model output

A model gets **no database handle, no filesystem access, no shell, and no
network**. It returns structured proposals; `app/agents/base/runner.py` decides
whether any of them may happen. Refused before anything is written:

* an action type outside the closed `ActionType` set;
* a payload that fails its Pydantic model (`extra="forbid"` throughout, so a
  model cannot widen the surface by inventing fields);
* a technical claim with no evidence anchor;
* an anchor id that does not belong to this project — a hallucinated id or a
  cross-project reference, and either way not evidence;
* a safety- or damage-related recommendation marked as not requiring approval;
* a senior review that tries to *raise* a confidence.

Refusals are recorded on the run and audited, not discarded. What a model tried
to do is part of the record.

**Prompt injection** is contained by the same boundary. A malicious instruction
embedded in an uploaded document can at most cause an agent to *propose*
something; it cannot widen the action set, forge an anchor, bypass the approval
gate, or reach anything outside the project. The Project Manager Agent cannot
create a finding at all, so it cannot be talked into an engineering conclusion.

## Authentication and authorisation

* Argon2id password hashing. Plaintext is never stored, logged, or written to an
  audit entry.
* HS256 tokens carrying a `jti`, deny-listed on logout — so signing out genuinely
  invalidates rather than gesturing at the client.
* Algorithm, issuer and audience are all pinned on decode, which is what stops
  the `alg: none` and algorithm-confusion attacks.
* Login failure is uniform: unknown email, wrong password and disabled account
  return the same message, and an unknown account still pays for a real hash
  verification so response timing cannot enumerate addresses.
* Two authorisation layers, both required: a global role and per-project
  membership. A global `engineer` role does not reach another engineer's project.
* "Not a member" and "wrong role" return the same 403, so project ids cannot be
  enumerated.
* Deciding an approval needs the reviewer role *and* a reviewing project role.
* The WebSocket repeats the same authorisation as the REST API — an upgrade
  request is not trusted just because it reached the endpoint.

## Audit

Append-only, written in the **same transaction** as the change it describes: an
operation that rolls back cannot leave an entry claiming it happened, and an
operation cannot succeed without its entry. Credential-shaped keys are redacted
before storage. Covers project lifecycle, file upload/rejection/deletion, login
success and failure, logout, user and permission changes, agent actions and
refusals, findings, approvals and reports.

## Secrets

No secret is committed. `.env.example` is the only checked-in configuration and
contains no real credential. `SecretStr` keeps keys out of logs and reprs.
`ESCO_ENVIRONMENT=production` refuses to start with the default JWT secret or the
default bootstrap password.

## Data residency

By default nothing leaves the machine: SQLite on disk, local artifact storage,
and the MockProvider. Ollama keeps content local too. Enabling OpenAI or
Anthropic sends project content to a third party, which is why it takes an
explicit `ESCO_ENABLED_PROVIDERS` entry *and* a credential, why there is no
fallback from a failing local provider to a cloud one, and why
`/api/system/capabilities` reports it in those words.

## Known limitations

Stated rather than glossed:

* **Read-only sealing is best-effort.** Some network and container filesystems
  ignore `chmod`; a warning is logged when it fails, and immutability is then a
  convention rather than an enforced property.
* **No rate limiting on login.** A local single-user deployment does not need it;
  an internal multi-user install behind a reverse proxy should add it there.
* **The audit log is append-only by construction, not by database permission.**
  Anyone with direct database access can alter it.
* **PostgreSQL is untested in CI.** The models use portable types and the intent
  is full support, but only SQLite is exercised.
* **`_HANDLERS` and the plugin registry are process-global.** Fine for the
  intended deployment; a plugin is trusted code once installed, and a malicious
  one has the same access as the application.
* **The deterministic MockProvider is not a model.** It restates what the
  evidence graph established. Every response says so and its confidence is
  capped, but a demo run must not be mistaken for an engineering review.

## Reporting

This is internal ESCO software. Report a security concern through the internal
engineering channel rather than a public issue tracker.
