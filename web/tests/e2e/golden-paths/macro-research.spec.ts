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
  await expect(page.getByRole("link", { name: "返回主线总览" })).toHaveAttribute("href", "/macro");
  await expect(page.getByText("CLAIM 1")).toBeVisible();
  await expect(page.getByRole("heading", { name: "资产影响" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "核心矛盾与解决条件" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "十二资产条件附录" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "备选解释：增长重新加速吸收利率冲击" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "已发布研究叙事" })).toHaveCount(0);
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
