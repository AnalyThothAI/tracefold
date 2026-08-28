import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsQuoteFixture } from "@tests/fixtures/newsFixture";

/**
 * The two market meanings stay visibly apart (#88).
 *
 * A rolling 24 h change read as an Event's reaction is the misreading this whole plane exists to prevent,
 * and the console's defence against it is layout: the current quote sits with the assets it belongs to, and
 * the fixed post-Event return is a block of its own with its own label.
 *
 * These two checks used to ride in `price-review.spec.ts` beside the ReviewDesk route's own golden path.
 * #256 removed that route; the invariants outlived it, so they moved here rather than going with it.
 *
 * @responsive-spec — both checks are about how two market blocks sit *beside* each other, which is a
 * desktop question; the spec sets its own viewport rather than trusting a project default.
 */
test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-07-23T10:00:00Z"));
  await installMockApi(page);
});

test("keeps the current quote and the post-Event reaction visibly apart on an Event", async ({
  page,
}) => {
  await page.setViewportSize({ height: 900, width: 1440 });
  await page.goto("/news/events/evt-global-policy");

  /*
   * Two market blocks, never one table. A rolling 24H change and a return anchored at this headline are
   * different time semantics, and a single table would invite reading the first as the market's answer to
   * the second.
   */
  const quotes = page.locator(".news-detail-hero .news-quote-table");
  await expect(quotes).toBeVisible();
  await expect(quotes).toContainText("现价");
  await expect(quotes).toContainText("24H");
  await expect(quotes).toContainText("不是事件时点的回填收益");
  const reactions = page.getByLabel("事件后反应");
  await expect(reactions).toBeVisible();
  await expect(reactions.locator(".news-detail-hero")).toHaveCount(0);
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
    const symbols = (new URL(route.request().url()).searchParams.get("symbols") ?? "")
      .split(",")
      .filter(Boolean);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: {
          measured_at_ms: Date.now(),
          quotes: symbols.map((symbol) =>
            newsQuoteFixture({
              base_symbol: symbol,
              effective_age_ms: 90_000,
              received_age_ms: 90_000,
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
    const symbols = (new URL(route.request().url()).searchParams.get("symbols") ?? "")
      .split(",")
      .filter(Boolean);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: {
          measured_at_ms: Date.now(),
          quotes: symbols.map((symbol) =>
            newsQuoteFixture({
              base_symbol: symbol,
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
