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
    page.getByRole("heading", {
      level: 2,
      name: "真实利率回落正在缓和风险资产的贴现压力",
    }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "返回当前主线" })).toHaveAttribute("href", "/macro");
  await expect(
    page.getByRole("heading", { name: "十二资产事实固定呈现，展望只在有传导时出现" }),
  ).toBeVisible();
  await expect(page.locator("[data-asset-fact]")).toHaveCount(12);
  await expect(page.getByRole("heading", { name: "证据、缺口与生成身份" })).toBeVisible();
  await expect(page.getByText("2 条实际引用证据")).toBeVisible();
  await expect(page.getByText("0 个冻结缺口")).toBeVisible();
  await expect(page.getByText("macro_thesis_workflow_v2")).toBeVisible();
  await expect(page.getByText("test-model")).toBeVisible();
  await expect(page.getByRole("heading", { name: "不可变 publication 历史" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行状态与四类 gate" })).toBeVisible();

  await expect(page.locator(".macro-research-workbench")).not.toContainText(
    /macro_daily_judgment|licensed_unavailable|Reviewer|买入|卖出|仓位/,
  );
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".macro-research-workbench",
    ".macro-research-citations",
    { selector: ".macro-thesis-report__asset-table", allowHorizontalOverflow: true },
    { selector: ".macro-thesis-report__recovery", allowHorizontalOverflow: true },
  ]);
  await expectNoUnhandledApiRequests(page);
});
