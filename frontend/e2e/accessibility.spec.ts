import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const screens = [
  ["city", "/?demo=1#/"],
  ["findings", "/?demo=1#/findings"],
  ["approvals", "/?demo=1#/approvals"],
  ["release", "/?demo=1#/release"],
  ["settings", "/?demo=1#/settings"]
] as const;

for (const [name, path] of screens) {
  test(`${name} has no serious WCAG A/AA violations`, async ({ page }) => {
    await page.goto(path);
    await expect(page.locator(".app")).toBeVisible();
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    const serious = results.violations.filter((violation) =>
      violation.impact === "serious" || violation.impact === "critical"
    );
    expect(serious, serious.map((item) => `${item.id}: ${item.help}`).join("\n")).toEqual([]);
  });
}
