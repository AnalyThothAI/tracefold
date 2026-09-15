import { expect, test } from "@tests/e2e/fixtures";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import {
  newsWalletDecimalDetailFixture,
  newsWalletEventFixture,
  newsWalletsFixture,
} from "@tests/fixtures/newsFixture";

test.beforeEach(async ({ page }) => {
  await installMockApi(page);
});

test("token episode keeps initial facts, current changes and unknown price separate", async ({
  page,
}, testInfo) => {
  await page.goto("/news/wallets");
  await expect(page.getByText("XYZ", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "XYZ", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "合格钱包与买卖净额 · 初始触发快照" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前变化与缺口" })).toBeVisible();
  await expect(page.getByRole("region", { name: "事件详情" })).not.toContainText(
    /已发送\s*·\s*未发送/,
  );
  await expect(page.getByText("未取得触发时的可靠价格基准，价格变化保持未知。")).toBeVisible();
  await expect(page.getByText("1.234567890123456789").first()).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("wallet-net-buy.png"), fullPage: true });
});

test("episode deep link survives reload independently of list and history range", async ({
  page,
}) => {
  const event = newsWalletEventFixture();
  await page.goto(`/news/wallets?history_range=7d&episode=${event.episode_id}`);
  await expect(page.getByRole("region", { name: "事件详情" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("region", { name: "事件详情" })).toBeVisible();
  await page.getByRole("button", { name: "关闭详情" }).click();
  expect(new URL(page.url()).searchParams.get("history_range")).toBe("7d");
  await expectNoDocumentHorizontalOverflow(page);
});

test("signed net amounts, zero return and tiny prices survive the real page", async ({
  page,
}, testInfo) => {
  const data = newsWalletDecimalDetailFixture();
  await page.route("**/api/news/wallets/events/*", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, data }) }),
  );
  await page.goto("/news/wallets?episode=" + data.event.episode_id);
  await page.getByText("其他观察地址与未纳入原因 · 1", { exact: true }).click();
  await expect(page.getByRole("cell", { name: "$-1,500.50 -1500 raw", exact: true })).toBeVisible();
  await expect(page.getByText("$0", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("价格 $1.23e-28", { exact: true })).toBeVisible();
  await expect(page.getByText("观察后价格变化 0%", { exact: true })).toBeVisible();
  const snapshotEdges = await page
    .locator(".news-wallets-snapshots > section")
    .evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().right));
  expect(Math.max(...snapshotEdges)).toBeLessThanOrEqual(page.viewportSize()!.width);
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("wallet-net-buy-values.png"), fullPage: true });
});

test("the status block leads the page and says why an untriggerable roster produced nothing", async ({
  page,
}, testInfo) => {
  const base = newsWalletsFixture();
  await page.route(/\/api\/news\/wallets(\?.*)?$/, (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: {
          ...base,
          roster: {
            ...base.roster,
            quality_count: 1,
            whale_count: 147,
            supported_quality_count: 1,
          },
          thresholds: { fast_n: 3, slow_n: 5, sufficient: false },
          funnel: { ...base.funnel, events: 0, intents: 0, sent: 0 },
        },
      }),
    }),
  );
  await page.goto("/news/wallets");
  const status = page.getByRole("region", { name: "名单与采集状态" });
  await expect(status).toBeVisible();
  await expect(status).toHaveAttribute("data-status-state", "roster_insufficient");
  await expect(status).toContainText(
    "当前质量地址 1 个，低于 5m 3 个及 30m 5 个门槛；当前名单不足以触发",
  );
  await expect(status).toContainText("1 / 147");
  // It leads the page: the block sits above the episode list rather than under it.
  const events = page.getByRole("region", { name: "集中净买入事件" });
  const [statusTop, eventsTop] = await Promise.all([
    status.evaluate((node) => node.getBoundingClientRect().top),
    events.evaluate((node) => node.getBoundingClientRect().top),
  ]);
  expect(statusTop).toBeLessThan(eventsTop);
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("wallet-status-block.png"), fullPage: true });
});
