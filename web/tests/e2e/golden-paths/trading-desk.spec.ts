import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import {
  tradingExecutionRowFixture,
  tradingExecutionsFixture,
} from "@tests/fixtures/tradingFixture";

test("positions lead the desk; execution opens a keyboard-dismissible Case and restores focus", async ({
  page,
}, testInfo) => {
  await installMockApi(page);
  await page.goto("/trading");
  await expect(page.getByRole("heading", { name: "交易执行" })).toBeVisible();
  const safety = page.getByLabel("执行安全状态");
  await expect(safety.getByText("执行服务在线")).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前仓位与保护" })).toBeVisible();
  for (const name of ["暂停新入场", "恢复新入场", "平掉账户仓位"]) {
    await expect(page.getByRole("button", { name })).toHaveCount(0);
  }
  const blocks = await page
    .locator("[data-block]")
    .evaluateAll((elements) => elements.map((e) => e.getAttribute("data-block")));
  expect(blocks).toEqual(["safety", "exposure", "tally"]);
  await page.getByRole("button", { name: "执行记录", exact: true }).click();
  await expect(page.locator(".trading-ledger-row")).toHaveCount(4);
  await expect(page.getByText("冻结止损 200 bps").first()).toBeVisible();
  await expect(page.getByText(/止盈 200 bps/).first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("trade-plan-ledger.png"), fullPage: true });
  await expectNoDocumentHorizontalOverflow(page);
  const signal = page.getByRole("button", { name: "crypto:perp:BTC:USDT" });
  await signal.click();
  const drawer = page.getByRole("dialog", { name: "策略判定依据" });
  await expect(drawer).toContainText("case-btc");
  expect(new URL(page.url()).searchParams.get("case")).toBe("case-btc");
  await expectNoDocumentHorizontalOverflow(page);
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(signal).toBeFocused();
  expect(new URL(page.url()).searchParams.get("tab")).toBe("executions");
  await page.getByRole("button", { name: "策略判定", exact: true }).click();
  await expect(page.getByRole("heading", { name: "最近 24 小时 · 判定分布" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("missing PnL and cold cost basis stay explicit in the existing desk", async ({
  page,
}, testInfo) => {
  await installMockApi(page);
  const data = tradingExecutionsFixture();
  data.totals = {
    ...data.totals,
    realized_known_today_usd: null,
    realized_known_total_usd: null,
    closed_today: 3,
    closed_total: 3,
    pnl_known_today: 0,
    pnl_known_total: 0,
    pnl_missing_today: 3,
    pnl_missing_total: 3,
    pnl_complete_today: false,
    pnl_complete_total: false,
  };
  data.executions = [
    tradingExecutionRowFixture({
      pnl_known: false,
      realized_pnl_usd: null,
      history_complete: false,
      gap_reason: "native_pnl_basis_incomplete_after_restart",
    }),
  ];
  await page.route("**/api/trading/executions*", (route) =>
    route.fulfill({ json: { ok: true, data } }),
  );
  await page.goto("/trading");
  await expect(page.getByText("平仓 3 · 已知 0 · 缺失 3")).toHaveCount(2);
  await expect(page.getByText(/不能视为账户完整净利润/)).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await page.getByRole("button", { name: "执行记录", exact: true }).click();
  await expect(page.getByText("盈亏未知")).toBeVisible();
  await expect(page.getByText("重启后成交成本基础不完整")).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({
    path: testInfo.outputPath("trade-plan-history-gap.png"),
    fullPage: true,
  });
  await expectNoUnhandledApiRequests(page);
});
