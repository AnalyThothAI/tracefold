import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsQuoteFixture } from "@tests/fixtures/newsFixture";

/** Current quote freshness and layout remain visible after retiring event returns.
 * @responsive-spec — quote tables are verified with an explicit desktop viewport.
 */
test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-07-23T10:00:00Z"));
  await installMockApi(page);
});

test("keeps current quotes on an Event after retiring event returns", async ({ page }) => {
  await page.setViewportSize({ height: 900, width: 1440 });
  await page.goto("/news/events/evt-global-policy?tab=market");

  const quotes = page.locator("#news-market .news-quote-table");
  await expect(quotes).toBeVisible();
  await expect(quotes).toContainText("现价");
  await expect(quotes).toContainText("24H");
  await expect(quotes).toContainText("不是事件时点的回填收益");
  await expect(page.getByLabel("事件后反应")).toHaveCount(0);
  await expectNoUnhandledApiRequests(page);
});

test("the topbar never promotes post-event price into a learning score", async ({ page }) => {
  await page.setViewportSize({ height: 900, width: 1440 });
  await page.goto("/news");

  await expect(page.locator(".topbar-figures").getByText("HIT 1H")).toHaveCount(0);
  await expect(page.locator(".topbar-figures").getByText("PUSHED 24H")).toBeVisible();
});

test("keeps a stale quote explicit and the dense Feed inside every viewport", async ({ page }) => {
  await page.route("**/api/news/quotes?**", async (route) => {
    const assets = JSON.parse(
      new URL(route.request().url()).searchParams.get("assets") ?? "[]",
    ) as Array<{ symbol: string; market_type: ReturnType<typeof newsQuoteFixture>["market_type"] }>;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: {
          measured_at_ms: Date.now(),
          quotes: assets.map(({ symbol, market_type }) =>
            newsQuoteFixture({
              base_symbol: symbol,
              effective_age_ms: 90_000,
              received_age_ms: 90_000,
              market_type,
              requested_symbol: symbol,
              state: "stale",
              state_zh: "报价陈旧",
              symbol,
              venue_symbol: `${symbol}USDT`,
            }),
          ),
        },
      }),
    });
  });
  await page.goto("/news");

  const price = page.locator(".news-event-list .news-quote-price").first();
  await expect(price).toBeVisible();
  await expect(page.locator(".news-event-list .news-quote-stale").first()).toHaveText("陈旧 2m");
  await expect(price).toHaveAttribute("title", /提供方时间.*Tracefold 接收.*24H 参考.*有效时效/);
  await expectNoDocumentHorizontalOverflow(page);
});

test("marks a failed quote refresh without rewriting its fresh LKG state", async ({ page }) => {
  let quoteCalls = 0;
  await page.route("**/api/news/quotes?**", async (route) => {
    quoteCalls += 1;
    if (quoteCalls > 1) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ok: false, error: "quote read failed" }),
      });
      return;
    }
    const assets = JSON.parse(
      new URL(route.request().url()).searchParams.get("assets") ?? "[]",
    ) as Array<{ symbol: string; market_type: ReturnType<typeof newsQuoteFixture>["market_type"] }>;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: {
          measured_at_ms: Date.now(),
          quotes: assets.map(({ symbol, market_type }) =>
            newsQuoteFixture({
              base_symbol: symbol,
              market_type,
              requested_symbol: symbol,
              symbol,
              venue_symbol: `${symbol}USDT`,
            }),
          ),
        },
      }),
    });
  });
  await page.goto("/news");

  const price = page.locator(".news-event-list .news-quote-price").first();
  await expect(price).toHaveAttribute("data-state", "fresh");
  await page.clock.fastForward(15_500);
  await expect.poll(() => quoteCalls).toBeGreaterThanOrEqual(2);
  await page.clock.fastForward(1_500);
  await expect.poll(() => quoteCalls).toBeGreaterThanOrEqual(3);

  await expect(
    page.getByRole("alert").filter({ hasText: "行情读取失败 · 上次成功于" }),
  ).toBeVisible();
  await expect(price).toHaveAttribute("data-state", "fresh");
  await expect(
    price.locator("xpath=ancestor::*[contains(@class, 'news-quote-read-failed')][1]"),
  ).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
});

test("uses the shared quote error state when no successful batch exists", async ({ page }) => {
  await page.route("**/api/news/quotes?**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: "quote read failed" }),
    });
  });
  await page.goto("/news");
  await page.clock.fastForward(1_500);

  await expect(page.getByRole("alert").filter({ hasText: "行情读取失败" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重试" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
});
