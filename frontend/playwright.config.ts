import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  retries: 1,
  use: { baseURL: "http://127.0.0.1:4173", trace: "on-first-retry" },
  webServer: { command: "npm run dev", url: "http://127.0.0.1:4173", reuseExistingServer: true },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }]
});
