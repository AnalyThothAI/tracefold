import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { CASE_ID } from "@tests/fixtures/tradingFixture";

test("LIVE scoreboard compares six policies with explicit insufficient data", async ({
  page,
}, testInfo) => {
  await installMockApi(page);
  await page.goto("/trading?tab=scoreboard");
  await expect(page.getByRole("heading", { name: "预测 → 决策 → 纸面 → 执行" })).toBeVisible();
  await expect(page.locator(".trading-scoreboard-table tbody tr")).toHaveCount(6);
  await expect(page.getByText("数据不足").first()).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("trading-scoreboard.png"), fullPage: true });
  await expectNoUnhandledApiRequests(page);
});

test("one frozen Case opens its forecast, six policy actions and paired paper legs", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/trading?tab=cases");
  await page.locator(".trading-case-list-row").first().click();
  const detail = page.getByRole("dialog", { name: "冻结 Case" });
  await expect(detail.getByText(CASE_ID)).toBeVisible();
  await expect(detail.getByText("两侧预测")).toBeVisible();
  await detail.getByRole("link", { name: "查看关联执行" }).click();
  await expect(page).toHaveURL(new RegExp(`execution_case=${CASE_ID}`));
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("an OI observation opens only its linked frozen Case", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news/market");
  const oi = page.locator('.news-market-row[data-kind="oi"]').first();
  await oi.getByRole("button", { expanded: false }).click();
  await page.getByRole("link", { name: "查看这条观察的策略判定" }).click();
  await expect(page).toHaveURL(/tab=cases&source_item_id=mkt-oi-wif-3/);
  await expect(page.getByText("按这条 OI 观察筛选")).toBeVisible();
  await expect(page.locator(".trading-case-list-row")).toHaveCount(1);
  await page.locator(".trading-case-list-row").click();
  await expect(page.getByRole("dialog", { name: "冻结 Case" })).toContainText(CASE_ID);
  await expectNoUnhandledApiRequests(page);
});
