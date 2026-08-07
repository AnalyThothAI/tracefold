import { expect, test, type Page } from "@playwright/test";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsFeedFixture } from "@tests/fixtures/newsFixture";

// @desktop-only-spec
test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("desktop-"), "desktop-only layout contract");
});

test("topbar keeps search and action controls contained", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 720 });
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator(".top-stats")).toHaveCount(0);
  await expect(page.locator(".searchbar")).toBeVisible();

  const layout = await page.evaluate(() => {
    const box = (selector: string) => {
      const element = document.querySelector(selector);
      if (!element) throw new Error(`Missing ${selector}`);
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        centerY: rect.top + rect.height / 2,
        width: rect.width,
      };
    };

    return {
      topbar: box(".topbar"),
      search: box(".searchbar"),
    };
  });

  expect(layout.search.width).toBeGreaterThanOrEqual(240);
  expect(layout.search.right).toBeLessThanOrEqual(layout.topbar.right);
  expect(layout.search.bottom).toBeLessThanOrEqual(layout.topbar.bottom);
  await expect(page.locator(".topbar-notification-slot")).toHaveCount(0);
});

test("1366x720 keeps at least four ordinary News cards fully visible", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 720 });
  await installMockApi(page);
  await routeNewsFeed(
    page,
    Array.from({ length: 6 }, (_, index) => `Ordinary high-signal News event ${index + 1}`),
  );
  await page.goto("/news");

  const rows = page.locator(".news-story-row");
  await expect(rows).toHaveCount(6);
  const fullyVisible = await rows.evaluateAll(
    (elements) =>
      elements.filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.top >= 0 && rect.bottom <= window.innerHeight;
      }).length,
  );

  expect(fullyVisible).toBeGreaterThanOrEqual(4);
});

test("a 668-character News feed title renders at no more than two lines", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 720 });
  await installMockApi(page);
  const longTitle = "长".repeat(668);
  await routeNewsFeed(page, [longTitle]);
  await page.goto("/news");

  const headline = page.locator(".news-story-title h2");
  await expect(headline).toHaveText(longTitle);
  const metrics = await headline.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clamp: style.webkitLineClamp,
      height: element.getBoundingClientRect().height,
      lineHeight: Number.parseFloat(style.lineHeight),
      textLength: element.textContent?.length ?? 0,
    };
  });

  expect(metrics.clamp).toBe("2");
  expect(metrics.textLength).toBe(668);
  expect(metrics.height).toBeLessThanOrEqual(metrics.lineHeight * 2 + 1);
});

async function routeNewsFeed(page: Page, titles: string[]) {
  const feed = newsFeedFixture();
  feed.stories = titles.map((title, index) => ({
    ...feed.stories[0],
    story_id: `news-density-${index + 1}`,
    title,
  }));
  await page.route("**/api/news/feed*", (route) =>
    route.fulfill({
      body: JSON.stringify({ data: feed, ok: true }),
      contentType: "application/json",
      status: 200,
    }),
  );
}
