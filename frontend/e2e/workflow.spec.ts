import { expect, test } from "@playwright/test";
test("synthetic engineering review workflow is operable", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("DEMO DATA")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Line 4 engineering campus" })).toBeVisible();
  await page.getByRole("button", { name: "Zoom in" }).click();
  await page.getByRole("button", { name: "Reset city view" }).click();
  await expect(page.getByRole("button", { name: "Automation Engineering, 1 active agents" })).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("link", { name: /Findings/ }).click();
  await page.getByText("F-107 · Photoeye PE-402").click();
  await expect(page.getByText(/field wiring/)).toBeVisible();
  await page.getByRole("link", { name: /Approvals/ }).click();
  await page.getByLabel("Decision").selectOption("Request clarification");
  await expect(page.getByRole("button", { name: "Submit signed decision" })).toBeEnabled();
  await page.getByRole("link", { name: "Reports" }).click();
  await expect(page.getByText("Detailed discrepancy report")).toBeVisible();
});
