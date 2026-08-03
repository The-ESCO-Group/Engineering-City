import { defineConfig, devices } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(frontendRoot, "..");
const backendRoot = path.join(repoRoot, "backend");
const python = path.join(repoRoot, ".venv", "Scripts", "python.exe");
const testResultsRoot = path.join(frontendRoot, "test-results");
const runRoot = process.env.ESCO_LIVE_E2E_RUN_ROOT ||
  path.join(testResultsRoot, "live", `backend-${process.pid}`);
const databasePath = path.join(runRoot, "live-e2e.sqlite3").replaceAll("\\", "/");
const frontendUrl = "http://127.0.0.1:4175";
const backendUrl = "http://127.0.0.1:8011";

const relativeRunRoot = path.relative(testResultsRoot, runRoot);
if (relativeRunRoot.startsWith("..") || path.isAbsolute(relativeRunRoot)) {
  throw new Error(`Refusing to reset unexpected live E2E path: ${runRoot}`);
}
mkdirSync(runRoot, { recursive: true });
process.env.ESCO_LIVE_E2E_RUN_ROOT = runRoot;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "live.spec.ts",
  outputDir: "test-results/live/results",
  retries: 0,
  workers: 1,
  timeout: 240_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: frontendUrl,
    trace: "retain-on-failure"
  },
  webServer: [
    {
      name: "local-backend",
      command: `"${python}" -m uvicorn app.main:app --host 127.0.0.1 --port 8011`,
      cwd: backendRoot,
      env: {
        ...process.env,
        ESCO_ENVIRONMENT: "demo",
        ESCO_DATABASE_URL: `sqlite+pysqlite:///${databasePath}`,
        ESCO_DATA_DIR: runRoot,
        ESCO_CORS_ORIGINS: JSON.stringify([frontendUrl]),
        ESCO_LOG_JSON: "false",
        ESCO_LOG_LEVEL: "WARNING",
        ESCO_WORKER_POLL_INTERVAL_SECONDS: "0.1",
        ESCO_JWT_SECRET: "live-e2e-local-secret-not-for-production"
      },
      url: `${backendUrl}/api/health`,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 120_000
    },
    {
      name: "live-frontend",
      command: "npm run dev -- --host 127.0.0.1 --port 4175 --strictPort",
      cwd: frontendRoot,
      env: {
        ...process.env,
        VITE_DEMO_MODE: "false",
        VITE_ESCO_API_BASE: `${backendUrl}/api`
      },
      url: frontendUrl,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 120_000
    }
  ],
  projects: [{
    name: "chromium-live-local",
    use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } }
  }]
});
