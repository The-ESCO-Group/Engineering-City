import { expect, test, type Page, type TestInfo } from "@playwright/test";

async function capture(page: Page, testInfo: TestInfo, name: string) {
  await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
}

test("synthetic city-to-release review workflow is operable and honest", async ({ page }, testInfo) => {
  await page.goto("/?demo=1#/");
  await expect(page.locator(".demo-badge")).toHaveText("DEMO DATA");
  await expect(page.getByRole("region", { name: "Engineering city workflow visualization" })).toBeVisible();
  await expect(page.getByRole("region", { name: "World event timeline" })).toContainText("SEQ 40");
  await expect(page.getByRole("complementary", { name: "Engineering city minimap" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Map", exact: true })).toHaveAttribute("aria-pressed", "true");

  const cityCanvas = page.locator("canvas.three-city-canvas");
  await expect(cityCanvas).toBeVisible();
  await page.getByRole("button", { name: "Pause timeline" }).click();
  const canvasBounds = await cityCanvas.boundingBox();
  if (!canvasBounds) throw new Error("The 3D city canvas did not expose interaction bounds.");
  const automationDistrict = page.getByRole("button", { name: /Automation Core: Logic review/ });
  const projectedPosition = () => automationDistrict.evaluate((element) => {
    const style = getComputedStyle(element);
    return `${style.getPropertyValue("--city-map-hud-x")}:${style.getPropertyValue("--city-map-hud-y")}`;
  });
  const initialLabelPosition = await projectedPosition();
  await page.mouse.move(canvasBounds.x + canvasBounds.width * .58, canvasBounds.y + canvasBounds.height * .48);
  await page.mouse.down();
  await page.mouse.move(canvasBounds.x + canvasBounds.width * .40, canvasBounds.y + canvasBounds.height * .48, { steps: 8 });
  await page.mouse.up();
  await expect.poll(projectedPosition, { timeout: 10_000 }).not.toBe(initialLabelPosition);
  await page.mouse.wheel(0, -620);
  await page.getByRole("button", { name: "Reset city view" }).click({ force: true });
  await page.getByRole("button", { name: "Mobility layer" }).click({ force: true });
  await expect(page.getByRole("button", { name: "Mobility layer" })).toHaveAttribute("aria-pressed", "true");
  await capture(page, testInfo, "city-map");

  await page.getByRole("button", { name: "Walk", exact: true }).click();
  await expect(page.getByRole("region", { name: "Engineering city workflow visualization" })).toHaveClass(/is-first-person/);
  await expect(page.getByRole("button", { name: /Exit walk/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reset walk position" })).toBeVisible();
  await page.keyboard.down("w");
  await page.waitForTimeout(120);
  await page.keyboard.up("w");
  await page.getByRole("button", { name: /Exit walk/ }).click({ force: true });
  await expect(page.getByRole("button", { name: "Map", exact: true })).toHaveAttribute("aria-pressed", "true");

  await page.goto("/?demo=1#/findings");
  await expect(page.getByRole("heading", { name: "Findings & evidence", level: 2 })).toBeVisible();
  await page.getByRole("button", { name: /DEMO-F-107/ }).click();
  await expect(page.getByText(/field wiring/i).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Evidence citations" })).toBeVisible();
  await capture(page, testInfo, "finding-evidence");

  await page.goto("/?demo=1#/approvals");
  await expect(page.getByRole("heading", { name: "Human decision inbox" })).toBeVisible();
  const reject = page.getByRole("button", { name: "Reject", exact: true }).first();
  await reject.focus();
  await page.keyboard.press("Enter");
  const comment = page.getByLabel(/Engineering comment/).first();
  await comment.fill("Rejected in the synthetic acceptance run after reviewing the cited evidence and stated limitations.");
  const submit = page.getByRole("button", { name: "Submit signed decision" }).first();
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByText(/Reject recorded for/)).toBeVisible();
  await capture(page, testInfo, "approval-decision");

  await page.goto("/?demo=1#/reports");
  await expect(page.getByRole("heading", { name: "Engineering reports" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Report library" })).toBeVisible();
  await expect(page.getByText(/0 of 19 required report types/)).toBeVisible();
  await expect(page.getByText(/No matching report record was returned by the API/)).toBeVisible();

  await page.goto("/?demo=1#/release/artifacts");
  await expect(page.getByRole("heading", { name: "Artifact package" })).toBeVisible();
  await expect(page.getByText(/No fake download is offered/)).toBeVisible();
  await expect(page.getByText(/no generation API calls/i)).toBeVisible();
  await capture(page, testInfo, "release-artifacts");

  await page.goto("/?demo=1#/settings");
  await expect(page.getByRole("heading", { name: "Desktop integration" })).toBeVisible();
  await expect(page.getByText("Native actions unavailable")).toBeVisible();
  await expect(page.getByRole("button", { name: "Locate support logs" })).toBeDisabled();
  await expect(page.getByText(/disabled in the browser/)).toBeVisible();
});

test("delivery engineering and operations surfaces remain usable on a compact laptop viewport", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });

  await page.goto("/?demo=1#/projects");
  await expect(page.locator(".filters output")).toHaveText("1 matching · 1 loaded");
  const projectSearch = page.getByRole("textbox", { name: "Search projects" });
  await projectSearch.fill("Palletizer");
  await expect(page.getByRole("heading", { name: "Line 4 Packaging Conveyor Upgrade" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "No matching projects" })).toBeVisible();
  await projectSearch.fill("Line 4");
  await expect(page.getByRole("heading", { name: "Line 4 Packaging Conveyor Upgrade" })).toBeVisible();
  await projectSearch.fill("");
  await page.getByRole("combobox", { name: "Filter project status" }).selectOption("In Progress");
  await expect(page.locator(".filters output")).toHaveText("1 matching · 1 loaded");
  const navigationDisclosure = page.getByRole("button", { name: "Primary navigation" });
  await expect(navigationDisclosure).toHaveAttribute("aria-expanded", "false");
  await expect(navigationDisclosure).toHaveAttribute("aria-controls", "primary-navigation");
  await navigationDisclosure.click();
  await expect(navigationDisclosure).toHaveAttribute("aria-expanded", "true");

  await page.goto("/?demo=1#/workspace/requirements?record=demo-req-002");
  await expect(page.getByRole("heading", { name: "URS-2.8", level: 3 })).toBeVisible();
  await expect(page.getByRole("link", { name: "Copyable deep link" })).toHaveAttribute("href", "#/workspace/requirements?record=demo-req-002");

  await page.goto("/?demo=1#/findings?record=DEMO-F-104&evidence=demo-ev-l5x-014");
  await expect(page.locator("#finding-DEMO-F-104")).toHaveClass(/selected/);
  await expect(page.getByRole("button", { name: /Synthetic MainProgram\.L5X/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("link", { name: "Evidence deep link" })).toHaveAttribute("href", "#/findings?record=DEMO-F-104&evidence=demo-ev-l5x-014");

  await page.goto("/?demo=1#/workspace/hmi");
  await expect(page.getByRole("heading", { name: "HMI engineering" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Line4_Overview" })).toBeVisible();

  await page.goto("/?demo=1#/workspace/alarms");
  await expect(page.getByRole("heading", { name: "Alarm engineering" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "L4-JAM-402" })).toBeVisible();

  await page.goto("/?demo=1#/workspace/tests");
  await expect(page.getByRole("heading", { name: "IAT and FAT procedures" })).toBeVisible();
  await expect(page.getByText(/No physical equipment operation is authorized/)).toBeVisible();

  await page.goto("/?demo=1#/workspace/documentation");
  await expect(page.getByRole("heading", { name: "Engineering documentation" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open Release Center" })).toBeEnabled();

  await page.goto("/?demo=1#/agents");
  await expect(page.getByRole("heading", { name: "Agent operations" })).toBeVisible();
  await expect(page.getByText(/no agent-control actions/i)).toBeVisible();

  await page.goto("/?demo=1#/audit");
  await expect(page.getByRole("heading", { name: "Audit Trail", level: 2 })).toBeVisible();
  await expect(page.getByText(/Recent stream window—not a separate audit-read service/)).toBeVisible();

  const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(horizontalOverflow).toBeLessThanOrEqual(1);
});

test.describe("reduced motion", () => {
  test.use({ reducedMotion: "reduce" });

  test("Release Center uses a direct-state replay", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/?demo=1#/release");
    await expect.poll(() => page.evaluate(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
    await expect(page.getByText("Reduced motion is active; replay uses a direct state highlight.")).toBeVisible();
  });
});
