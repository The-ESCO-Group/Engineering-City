import { expect, test } from "@playwright/test";
test("synthetic engineering review workflow is operable", async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto("/");
  await expect(page.locator(".connection")).toContainText("System status");
  await page.getByRole("button", { name: "Pause timeline" }).click();
  await expect(page.locator(".city-command-identity h2")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Engineering city minimap" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Synthetic demo workflow activity" })).toContainText("PLC Review");
  await expect(page.getByRole("button", { name: "Map", exact: true })).toHaveAttribute("aria-pressed", "true");
  const cityCanvas = page.locator("canvas.three-city-canvas");
  await expect(cityCanvas).toBeVisible();
  const canvasBounds = await cityCanvas.boundingBox();
  if (!canvasBounds) throw new Error("The 3D city canvas did not expose interaction bounds.");
  const automationLabel = page.getByRole("button", { name: "Automation Core: Logic review · 1 active" });
  const initialLabelPosition = await automationLabel.evaluate((element) => {
    const style = getComputedStyle(element);
    return `${style.left}:${style.top}`;
  });
  await page.mouse.move(
    canvasBounds.x + canvasBounds.width * 0.58,
    canvasBounds.y + canvasBounds.height * 0.48
  );
  await page.mouse.down();
  await page.mouse.move(
    canvasBounds.x + canvasBounds.width * 0.42,
    canvasBounds.y + canvasBounds.height * 0.48,
    { steps: 8 }
  );
  await page.mouse.up();
  await page.waitForTimeout(220);
  const orbitedLabelPosition = await automationLabel.evaluate((element) => {
    const style = getComputedStyle(element);
    return `${style.left}:${style.top}`;
  });
  expect(orbitedLabelPosition).not.toBe(initialLabelPosition);
  await page.mouse.wheel(0, -620);
  await page.waitForTimeout(220);
  const zoomedLabelPosition = await automationLabel.evaluate((element) => {
    const style = getComputedStyle(element);
    return `${style.left}:${style.top}`;
  });
  expect(zoomedLabelPosition).not.toBe(orbitedLabelPosition);
  await page.getByRole("button", { name: "Reset city view" }).click({ force: true });
  await expect(page.getByRole("button", { name: "Overview layer" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Mobility layer" }).click({ force: true });
  await expect(page.getByRole("button", { name: "Mobility layer" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Zoom map in", exact: true }).click({ force: true });
  await page.getByRole("button", { name: "Reset city view" }).click({ force: true });
  await expect(page.getByRole("button", { name: "Automation Engineering, 1 active agent" })).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".workspace")).toHaveCount(0);
  await expect(page.locator(".activity-panel")).toHaveCount(0);
  await page.getByRole("button", { name: "Walk", exact: true }).click();
  await expect(page.getByRole("region", { name: "Engineering city workflow visualization" })).toHaveClass(/is-first-person/);
  await expect(page.getByRole("status")).toContainText("CLICK CITY TO LOOK");
  await expect(page.getByRole("button", { name: "Exit walk Map view", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reset walk position", exact: true })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Engineering city minimap" })).not.toBeVisible();
  await page.keyboard.down("w");
  await page.waitForTimeout(120);
  await page.keyboard.up("w");
  await page.getByRole("button", { name: "Exit walk Map view", exact: true }).click({ force: true });
  await expect(page.getByRole("button", { name: "Map", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("complementary", { name: "Engineering city minimap" })).toBeVisible();
  await page.getByRole("button", { name: "Automation Core: Logic review · 1 active" }).click();
  await expect(page.getByRole("button", { name: "Automation Engineering, 1 active agent" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("link", { name: /Findings/ })).toHaveAttribute("href", "#/findings");
  await page.goto("/#/findings");
  await expect(page.locator(".findings-page")).toBeVisible();
  await page.getByText("F-107 · Photoeye PE-402").click();
  await expect(page.getByText(/field wiring/)).toBeVisible();
  await page.goto("/#/approvals");
  await page.getByLabel("Decision").selectOption("Accept");
  await page.getByLabel("Engineering rationale (required)").fill("Verified against the synthetic evidence set.");
  await expect(page.getByRole("button", { name: "Submit signed decision" })).toBeEnabled();
  await page.goto("/#/reports");
  await expect(page.getByText("Detailed discrepancy report")).toBeVisible();
});
