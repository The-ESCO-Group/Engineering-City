import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const BACKEND = "http://127.0.0.1:8011/api";
const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "fixtures",
  "demo_project",
  "Line3_Project_Package.zip"
);

interface ApprovalSummary {
  id: string;
  title: string;
  kind: string;
  status: string;
  decision_allowed: boolean;
}

interface ProjectSummary {
  id: string;
  name: string;
}

interface ReportSummary {
  id: string;
  status: string;
  download_path: string | null;
  size_bytes: number | null;
  sha256: string | null;
}

async function pendingApprovals(request: APIRequestContext, projectId: string): Promise<ApprovalSummary[]> {
  const response = await request.get(`${BACKEND}/projects/${projectId}/approvals`);
  expect(response.ok(), await response.text()).toBeTruthy();
  return ((await response.json()) as ApprovalSummary[]).filter((item) => item.status === "pending");
}

async function waitForEligibleFindingApproval(
  request: APIRequestContext,
  projectId: string,
  page: Page
): Promise<ApprovalSummary> {
  const deadline = Date.now() + 150_000;
  while (Date.now() < deadline) {
    const eligible = (await pendingApprovals(request, projectId)).find(
      (approval) => approval.kind !== "gate_decision" && approval.decision_allowed
    );
    if (eligible) return eligible;
    await page.waitForTimeout(500);
  }
  throw new Error("No finding approval became eligible after independent senior review ran.");
}

async function driveReviewToCompletion(
  request: APIRequestContext,
  projectId: string,
  page: Page
): Promise<void> {
  const deadline = Date.now() + 150_000;
  while (Date.now() < deadline) {
    const projectResponse = await request.get(`${BACKEND}/projects/${projectId}`);
    expect(projectResponse.ok(), await projectResponse.text()).toBeTruthy();
    const project = (await projectResponse.json()) as { status: string };
    if (project.status === "completed") return;
    if (["failed", "cancelled"].includes(project.status)) {
      throw new Error(`Workflow entered terminal status '${project.status}'.`);
    }

    for (const approval of await pendingApprovals(request, projectId)) {
      if (!approval.decision_allowed) continue;
      expect(approval.kind, "The review-only acceptance run must not create production gate approvals.")
        .not.toBe("gate_decision");
      const response = await request.post(`${BACKEND}/approvals/${approval.id}/decision`, {
        data: {
          approved: true,
          notes: "Approved for this isolated synthetic run after reviewing the published evidence and limitations."
        }
      });
      expect(response.ok(), await response.text()).toBeTruthy();
    }
    await page.waitForTimeout(500);
  }
  throw new Error("Review-only workflow did not complete after all eligible decisions were recorded.");
}

async function generatedReports(request: APIRequestContext, projectId: string): Promise<ReportSummary[]> {
  const response = await request.get(`${BACKEND}/projects/${projectId}/reports`);
  expect(response.ok(), await response.text()).toBeTruthy();
  return ((await response.json()) as ReportSummary[]).filter((item) => item.status === "generated");
}

async function projectIdByName(request: APIRequestContext, name: string): Promise<string> {
  const response = await request.get(`${BACKEND}/projects?limit=500`);
  expect(response.ok(), await response.text()).toBeTruthy();
  const body = (await response.json()) as { items: ProjectSummary[] };
  return body.items.find((project) => project.name === name)?.id ?? "";
}

async function signIn(page: Page): Promise<void> {
  await page.goto("/#/settings");
  const submit = page.getByRole("button", { name: "Sign in to local backend" });
  await expect(page.getByLabel("Email")).toBeEnabled({ timeout: 60_000 });
  await page.getByLabel("Email").fill("admin@esco.internal");
  await page.getByLabel("Password").fill("esco-demo-admin");
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByText("ESCO Administrator").first()).toBeVisible();
}

test("real local workflow persists intake, human decisions, and downloadable reports", async ({ page }) => {
  test.setTimeout(300_000);
  await signIn(page);
  await page.evaluate(() => localStorage.removeItem("esco:intake-draft:v1"));
  await page.goto("/#/workspace/intake");

  await page.getByLabel("Project name").fill("Live E2E Conveyor Review");
  await page.getByLabel("Customer / internal label").fill("SYNTHETIC-E2E");
  await page.getByLabel("Plant / site").fill("Synthetic Test Plant");
  await page.getByLabel("Project objective").fill(
    "Review the synthetic Line 3 package, preserve exact evidence, require human decisions, and produce local reports."
  );

  await page.getByLabel("Controller family").selectOption({ label: "CompactLogix" });
  await page.getByLabel("Catalog number").fill("5069-L306ER");
  await page.getByLabel("Firmware").fill("35");
  await page.getByLabel("Studio 5000 version").fill("35");
  await page.getByText("Advanced project settings").click();
  await page.getByLabel("Release mode").selectOption("review_only");

  await page.locator('input[type="file"]').setInputFiles(FIXTURE);
  await expect(page.getByText("Line3_Project_Package.zip")).toBeVisible();

  await page.getByLabel(/Engineering responsibility acknowledged/).check();
  const create = page.getByRole("button", { name: "Create project & start agents" });
  await expect(create).toBeEnabled();
  await create.click();

  await expect(page.getByLabel("Engineering city workflow visualization")).toBeVisible({ timeout: 90_000 });
  let projectId = "";
  await expect.poll(
    async () => {
      projectId = await projectIdByName(page.request, "Live E2E Conveyor Review");
      return projectId;
    },
    { timeout: 90_000, intervals: [500, 1000, 2000] }
  ).not.toBe("");
  const projectResponse = await page.request.get(`${BACKEND}/projects/${projectId}`);
  expect(projectResponse.ok(), await projectResponse.text()).toBeTruthy();
  const project = (await projectResponse.json()) as {
    attributes?: { workflow?: { kind?: string } };
  };
  expect(project.attributes?.workflow?.kind).toBe("review");

  await page.goto("/#/tasks");
  await expect(page.getByRole("heading", { name: "Task board & list" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Inventory uploaded files/ }).first(),
  ).toBeVisible({ timeout: 60_000 });

  await page.reload();
  await expect(page.locator(".project-switch")).toContainText("Live E2E Conveyor Review");
  await expect(page.locator(".demo-badge")).toHaveText("LIVE LOCAL DATA");

  const firstEligibleApproval = await waitForEligibleFindingApproval(page.request, projectId, page);

  await page.goto("/#/approvals");
  await expect(page.getByRole("heading", { name: "Human decision inbox" })).toBeVisible();
  const firstRequest = page.locator(".eng-approval-list button").filter({ hasText: firstEligibleApproval.title }).first();
  await expect(firstRequest).toBeVisible();
  await firstRequest.click();
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await page.getByLabel(/Engineering comment/).fill(
    "Approved for this synthetic local E2E run after reviewing the published evidence and limitations."
  );
  await page.getByRole("button", { name: "Submit signed decision" }).click();
  await expect(page.getByText(/Approve recorded for/)).toBeVisible();

  await driveReviewToCompletion(page.request, projectId, page);

  await expect.poll(
    async () => (await generatedReports(page.request, projectId)).length,
    { timeout: 150_000, intervals: [500, 1000, 2000] }
  ).toBeGreaterThan(0);

  const reports = await generatedReports(page.request, projectId);
  const actualReport = reports.find((item) => item.download_path && item.size_bytes && item.sha256);
  expect(actualReport).toBeTruthy();
  const artifactResponse = await page.request.get(new URL(actualReport!.download_path!, BACKEND).toString());
  expect(artifactResponse.ok(), await artifactResponse.text()).toBeTruthy();
  expect((await artifactResponse.body()).byteLength).toBe(actualReport!.size_bytes);

  await page.goto("/#/reports");
  await expect(page.getByRole("heading", { name: "Engineering reports" })).toBeVisible();
  const generatedCatalogItem = page.locator(".release-report-layout nav button").filter({ hasText: /generated format/i }).first();
  await expect(generatedCatalogItem).toBeVisible();
  await generatedCatalogItem.click();
  const downloadLink = page.getByRole("link", { name: /Download generated/ }).first();
  await expect(downloadLink).toBeVisible();
  const [download] = await Promise.all([page.waitForEvent("download"), downloadLink.click()]);
  const downloadedPath = await download.path();
  expect(downloadedPath).toBeTruthy();
  expect((await stat(downloadedPath!)).size).toBeGreaterThan(100);

  const completedProject = await page.request.get(`${BACKEND}/projects/${projectId}`);
  expect(completedProject.ok(), await completedProject.text()).toBeTruthy();
  expect(((await completedProject.json()) as { status: string }).status).toBe("completed");
});
