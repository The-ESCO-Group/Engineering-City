$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { throw "Rust is required." }
if (-not (Get-Command cargo-tauri -ErrorAction SilentlyContinue)) { throw "Tauri CLI is required: cargo install tauri-cli --version '^2.0'." }
Push-Location (Join-Path $RepoRoot "desktop\src-tauri")
try { cargo tauri build } finally { Pop-Location }

