# Local desktop and Windows operations

The Tauri shell is a local presentation layer for ESCO Engineering City. It does not start a cloud service, embed secrets, or expose controller upload/download, online edit, force, or live-equipment control.

## Setup and daily use

PowerShell 7, Node.js 20+, npm, and Python 3.12+ are required for the complete local stack. Rust is required for desktop checks and builds. The Tauri CLI is pinned in `desktop/package.json` and installed beneath `desktop/node_modules`; no global Tauri installation is required.

```powershell
# Repository-local Node/Python dependencies; no global package changes.
.\scripts\product\setup.ps1

# Local FastAPI backend first, then Vite, with supervised logs and health checks.
.\scripts\product\dev.ps1

# Fully offline synthetic UI. No backend or provider is started.
.\scripts\product\run-demo.ps1
```

`dev.ps1` binds both services to `127.0.0.1`, waits for `/api/health`, and then opens the frontend. Use `-BackendPort` or `-FrontendPort` to choose non-default local ports. Use `-DataDirectory data/local-live` to preserve the default database and launch a separate persistent local workspace; the launcher keeps that workspace's SQLite file and project artifacts together and never resets them. It prints every endpoint and stores process output under `logs/product/`. Ctrl+C stops only the child processes that the script created.

The packaged Tauri application does not bundle, install, or supervise the Python backend. `dev.ps1` provides backend/frontend supervision for local development; a packaged workstation needs its separately administered compatible local backend.

Every native command is checked explicitly for a non-zero exit code. A failed prerequisite, health check, frontend build, Rust test, or package step fails the script instead of continuing with a false success message.

## Local backend policy

The desktop shell parses backend URLs structurally. Only plain HTTP loopback hosts are accepted: `localhost`, IPv4 loopback, or `::1`. URL credentials, query strings, fragments, hostname lookalikes, and remote hosts are rejected.

The default backend is `http://127.0.0.1:8000`. Administrators may set `ESCO_BACKEND_URL` to another loopback origin for shell diagnostics, and `build-desktop.ps1 -BackendUrl http://127.0.0.1:9000` compiles a different local API endpoint into a workstation-specific package. Provider credentials remain backend-only environment values; never prefix a secret with `VITE_`, because Vite variables become browser assets.

When an independently managed backend serves a packaged WebView, its `ESCO_CORS_ORIGINS` must explicitly include the desktop origin used on that workstation (normally `http://tauri.localhost`). `dev.ps1` includes the known Tauri local origins automatically. Do not use a wildcard origin.

## Authorized project and artifact opening

The shell does not expose Tauri's general filesystem opener. Custom commands resolve canonical paths and permit only:

- a project directory that is a direct child of an authorized projects root;
- a generated artifact below an authorized project with one of these extensions: CSV, DOCX, HTML, JSON, L5X, PDF, TXT, XLSX, XML, or ZIP.

Set `ESCO_PROJECTS_DIR` to one or more absolute local roots using the platform path separator, or set absolute `ESCO_DATA_DIR` and the shell will authorize its `projects` child. With neither variable, the shell uses its application-data `projects` directory. UNC shares, device namespaces, symlink escapes, missing paths, nested directories presented as projects, and executable artifacts are rejected.

The Settings screen exposes this native boundary without imitating it in a browser. Native buttons stay disabled in web mode and explain that the installed desktop runtime is required.

## Operator notifications

Local OS notifications are deliberately narrow: the frontend may request one only when a human approval is pending or a workflow task has failed. Rust accepts only those two event kinds and bounds title/body length before using the notification plugin. Raw evidence, credentials, model reasoning, and arbitrary application messages are not sent to the OS notification surface.

## Validation, support, and packaging

```powershell
# PowerShell parser/security tests, frontend gates, backend tests, and Rust tests.
.\scripts\product\test.ps1

# Add browser E2E when Chromium is installed.
.\scripts\product\test.ps1 -IncludeE2E

# Exercise a fresh isolated SQLite backend through the real browser workflow.
.\scripts\product\test.ps1 -IncludeLiveE2E

# Install the pinned repository-local CLI and locked Rust dependencies.
.\scripts\product\setup.ps1 -SkipBackend -SkipFrontend -IncludeDesktop

# Build MSI and NSIS packages with the repository-local Tauri CLI 2.
.\scripts\product\build-desktop.ps1

# Build and package installers with manifest and SHA-256 checksums.
.\scripts\product\package-release.ps1 -Version 0.1.0

# Collect tailed service logs, native Tauri logs, and tool versions; excludes .env,
# databases, engineering files, artifacts, and environment values.
.\scripts\product\collect-support-logs.ps1
```

`setup.ps1 -IncludeDesktop` runs `npm ci --ignore-scripts` in `desktop/`, validates the exact CLI version and lockfile integrity, and fetches locked Rust dependencies. `build-desktop.ps1` executes the CLI entrypoint through the resolved Node executable and refuses to use a global `cargo tauri` fallback. Review changes to `desktop/package-lock.json` before dependency updates.

Desktop packaging is signing-independent by default. Internal release policy must apply any required code-signing step outside the repository without committing certificates or passwords. A packaged installer or ZIP is not a Rockwell import verification, a human approval, or a released engineering deliverable.

`package-release.ps1` always invokes a fresh desktop build, requires its requested version to match both Tauri and Cargo metadata, and packages only installers written by that invocation. Its manifest records the source commit, whether tracked changes were present, a hash of that tracked status, the pinned Tauri CLI version, artifact hashes, and the candidate-only verification boundary. `-SkipBuild` is rejected for release packaging.

Service logs under `logs/product/` and native Tauri logs under the application log directory are distinct sources. The support collector tails both when present and removes common authorization, cookie, secret, private-key, and user-profile patterns. Redaction is best-effort; `REVIEW-BEFORE-SHARING.txt` requires a human to inspect every bundle before it leaves the authorized engineering team.
