import { expect, test, type Page } from "@playwright/test";

test.use({ reducedMotion: "reduce", colorScheme: "dark" });

async function expectVisual(page: Page, name: string) {
  const screenshot = await page.screenshot({ animations: "disabled", fullPage: false });
  expect(screenshot).toMatchSnapshot(name, { maxDiffPixelRatio: .015 });
}

test("major product surfaces match the approved 1440×900 layout", async ({ page }) => {
  test.setTimeout(420_000);
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" });
  await page.goto("/?demo=1#/");
  await expect(page.locator("canvas.three-city-canvas")).toBeVisible();
  await page.getByRole("button", { name: "Pause timeline" }).click();
  await page.waitForTimeout(800);
  await expectVisual(page, "city-overview.png");

  await page.getByRole("button", { name: /Automation Core: Logic review/ }).click();
  await page.waitForTimeout(900);
  await expectVisual(page, "city-district-focus.png");

  await page.getByRole("button", { name: /Inspect PLC Logic Agent/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectVisual(page, "city-agent-inspector.png");

  await page.goto("/?demo=1#/workspace/requirements");
  await expect(page.locator(".eng-workspace-main")).toBeVisible();
  await expectVisual(page, "project-core-requirements.png");

  await page.goto("/?demo=1#/findings");
  await page.getByRole("button", { name: /DEMO-F-107/ }).click();
  await expect(page.getByRole("region", { name: "Evidence citations" })).toBeVisible();
  await expectVisual(page, "finding-detail.png");

  await page.goto("/?demo=1#/approvals");
  await expect(page.getByRole("heading", { name: "Human decision inbox" })).toBeVisible();
  await expectVisual(page, "approval-inbox.png");

  await page.goto("/?demo=1#/release");
  await expect(page.getByRole("heading", { name: "Release readiness", level: 1 })).toBeVisible();
  await expectVisual(page, "release-center.png");

  await page.goto("/?demo=1#/settings");
  await expect(page.getByRole("heading", { name: "Desktop integration" })).toBeVisible();
  await expectVisual(page, "settings-desktop-boundary.png");
});
