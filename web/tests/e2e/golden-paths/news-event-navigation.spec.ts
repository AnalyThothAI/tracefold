import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsFeedEventFixture, newsFeedFixture } from "@tests/fixtures/newsFixture";

const eventPath = "/news/events/evt-global-policy";
const filteredFeedPath = "/news?q=tariff&outcome=held&hours=168&event_kind=news";

// Click, keyboard activation and independent URLs follow the same detail route at every viewport.
test("opens the canonical Event page directly on an ordinary headline click", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news");

  const headline = page.locator('[data-event-id="evt-global-policy"] h2 a');
  await expect(headline).toHaveAttribute("href", eventPath);
  await headline.click();

  await expect(page).toHaveURL(eventPath);
  const detail = page.getByRole("region", { name: "新闻事件详情" });
  await expect(detail.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "新闻事件流" })).toHaveCount(0);
  await expect(page.locator(".news-event-row")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  // Public asset navigation still follows the adopted asset, rather than raw source BTC/ETH mentions.
  await expect(
    detail
      .locator(".news-detail-hero")
      .getByLabel("关联资产")
      .getByRole("link", { name: "CL", exact: true }),
  ).toHaveAttribute("href", "/news/symbols/CL");
  await expect(
    detail.locator(
      '.news-detail-hero a[href="/news/symbols/BTC"], .news-detail-hero a[href="/news/symbols/ETH"]',
    ),
  ).toHaveCount(0);
  await expect(page.locator(".news-detail-pager")).toBeVisible();
  await expect(page.getByRole("link", { name: "返回新闻事件流" })).toHaveAttribute(
    "href",
    "/news?outcome=pushed&hours=24",
  );

  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("Enter opens the Event and the return link restores the feed filters", async ({ page }) => {
  await installMockApi(page);
  await page.goto(filteredFeedPath);

  const headline = page.locator('[data-event-id="evt-global-policy"] h2 a');
  await headline.focus();
  await expect(headline).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(eventPath);
  await expect(page.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  const back = page.getByRole("link", { name: "返回新闻事件流" });
  await expect(back).toHaveAttribute("href", filteredFeedPath);
  await back.click();
  await expect(page).toHaveURL(filteredFeedPath);
  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "news search" })).toHaveValue("tariff");
  await expect(page.getByRole("tab", { name: "未推送 271" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("button", { name: "时间范围，最近 7 天" })).toBeVisible();
  await expect(page.getByRole("button", { name: "筛选 · 1" })).toBeVisible();

  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("browser Back and Forward retain the Event and its filtered feed context", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto(filteredFeedPath);
  await page.locator('[data-event-id="evt-global-policy"] h2 a').click();
  await expect(page.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(filteredFeedPath);
  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "news search" })).toHaveValue("tariff");

  await page.goForward();
  await expect(page).toHaveURL(eventPath);
  await expect(page.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回新闻事件流" })).toHaveAttribute(
    "href",
    filteredFeedPath,
  );
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expectNoUnhandledApiRequests(page);
});

test("the native headline href cold-loads the Event without inventing a feed position", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto(filteredFeedPath);
  const headline = page.locator('[data-event-id="evt-global-policy"] h2 a');
  await expect(headline).toHaveAttribute("href", eventPath);
  const href = await headline.getAttribute("href");

  // A document navigation follows the published href with no SPA route state, like opening a shared URL.
  await page.goto(href!);
  await expect(page).toHaveURL(eventPath);
  await expect(page.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回新闻事件流" })).toHaveAttribute("href", "/news");
  await expect(page.locator(".news-detail-pager")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("opens details at the top and restores the feed reading position through Back and the return link", async ({
  page,
}) => {
  await installMockApi(page);
  await page.route("**/api/news/feed?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: newsFeedFixture({
          events: Array.from({ length: 25 }, (_, index) =>
            newsFeedEventFixture({
              event_id: `evt-scroll-${index + 1}`,
              leader_title: `Reading position event ${index + 1}`,
              update: null,
            }),
          ),
        }),
      }),
    });
  });
  await page.goto(filteredFeedPath);

  const column = page.locator(".center-column");
  const headline = page.locator('[data-event-id="evt-scroll-12"] h2 a');
  await headline.scrollIntoViewIfNeeded();
  const readingTop = await column.evaluate((element) => element.scrollTop);
  expect(readingTop).toBeGreaterThan(100);
  await headline.click();
  await expect(page).toHaveURL("/news/events/evt-scroll-12");
  await expect(page.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();
  await expect.poll(() => column.evaluate((element) => element.scrollTop)).toBe(0);

  await page.goBack();
  await expect(page).toHaveURL(filteredFeedPath);
  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeAttached();
  await expect
    .poll(async () =>
      Math.abs((await column.evaluate((element) => element.scrollTop)) - readingTop),
    )
    .toBeLessThanOrEqual(1);

  await headline.click();
  await expect(page.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();
  await expect.poll(() => column.evaluate((element) => element.scrollTop)).toBe(0);
  await page.getByRole("link", { name: "返回新闻事件流" }).click();
  await expect(page).toHaveURL(filteredFeedPath);
  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeAttached();
  await expect
    .poll(async () =>
      Math.abs((await column.evaluate((element) => element.scrollTop)) - readingTop),
    )
    .toBeLessThanOrEqual(1);

  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("retains the latest feed window after resetting an evicted cursor chain and visiting details", async ({
  page,
}) => {
  await installMockApi(page);
  await page.route("**/api/news/feed?**", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    const pageNumber = cursor == null ? 0 : Number(cursor.slice("cursor-".length));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: newsFeedFixture({
          events: [
            newsFeedEventFixture({
              event_id: `evt-window-${pageNumber}`,
              leader_title:
                pageNumber === 0 ? "Latest feed window" : `Older window page ${pageNumber}`,
              update: null,
            }),
          ],
          next_cursor: pageNumber < 3 ? `cursor-${pageNumber + 1}` : null,
          counts: pageNumber === 0 ? newsFeedFixture().counts : null,
        }),
      }),
    });
  });
  await page.goto(filteredFeedPath);
  await expect(page.getByRole("heading", { name: "Latest feed window" })).toBeVisible();
  for (let pageNumber = 1; pageNumber <= 3; pageNumber += 1) {
    await page.getByRole("button", { name: "加载更多事件" }).click();
    await expect(
      page.getByRole("heading", { name: `Older window page ${pageNumber}` }),
    ).toBeVisible();
  }
  await expect(page.getByRole("heading", { name: "Latest feed window" })).toHaveCount(0);
  await page.getByRole("button", { name: "返回最新事件" }).click();
  await expect(page.getByRole("heading", { name: "Latest feed window" })).toBeVisible();
  await expect(page.locator(".news-event-row")).toHaveCount(1);

  await page.locator('[data-event-id="evt-window-0"] h2 a').click();
  await expect(page.getByRole("heading", { name: "钢铁进口关税上调至 50%" })).toBeVisible();
  await page.getByRole("link", { name: "返回新闻事件流" }).click();
  await expect(page).toHaveURL(filteredFeedPath);
  await expect(page.getByRole("heading", { name: "Latest feed window" })).toBeVisible();
  await expect(page.locator(".news-event-row")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "返回最新事件" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Older window page/ })).toHaveCount(0);
  await expectNoUnhandledApiRequests(page);
});
