import { expect, test } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test("renders one immutable Macro Thesis history workbench", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/macro/research");

  await expect(page.getByRole("heading", { level: 1, name: "Macro Thesis 档案" })).toBeVisible();
  await expect(
    page.getByRole("heading", { level: 2, name: "实际利率上行主导短期风险资产定价" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "主线论点与因果链" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "核心矛盾" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "十二资产：动量与条件展望" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "不可变主线历史" })).toBeVisible();

  const audit = page.locator(".macro-research-audit");
  await expect(audit).not.toHaveAttribute("open", "");
  await audit.locator("summary").click();
  await expect(audit).toHaveAttribute("open", "");
  await expect(page.getByText("macro_thesis_workflow_v1")).toBeVisible();
  await expect(page.getByText("research-fixture")).toBeVisible();
  await expect(audit).not.toContainText(/^\s*\{/);

  await expect(page.getByRole("main", { name: "Macro Thesis 档案" })).not.toContainText(
    /macro_daily_judgment|licensed_unavailable|买入|卖出|仓位/,
  );
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".macro-research-workbench",
    ".macro-research-document",
    ".macro-research-sections",
    ".macro-research-citations",
  ]);
  await expectNoUnhandledApiRequests(page);
});
