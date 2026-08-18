import { expect, test, type Page } from "@playwright/test";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsFeedEventFixture, newsFeedFixture } from "@tests/fixtures/newsFixture";

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

  const rows = page.locator(".news-event-row");
  await expect(rows).toHaveCount(6);
  await expect(page.getByRole("link", { name: "事件流" })).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".news-provider-score")).toHaveCount(6);
  await expect(page.locator(".news-provider-score").first()).toContainText("OpenNews88");
  const fullyVisible = await rows.evaluateAll(
    (elements) =>
      elements.filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.top >= 0 && rect.bottom <= window.innerHeight;
      }).length,
  );

  expect(fullyVisible).toBeGreaterThanOrEqual(4);
});

test("News missing-asset evidence remains readable at AA contrast", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 720 });
  await installMockApi(page);
  const feed = newsFeedFixture({
    events: [
      newsFeedEventFixture({
        title_zh: null,
        event_id: "news-assetless-contrast",
        grounded_assets: [],
        watchlist_hits: [],
      }),
      newsFeedEventFixture({
        title_zh: null,
        event_id: "news-grounded-contrast",
        grounded_assets: ["CL"],
        watchlist_hits: [],
      }),
    ],
  });
  await page.route("**/api/news/feed*", (route) =>
    route.fulfill({
      body: JSON.stringify({ data: feed, ok: true }),
      contentType: "application/json",
      status: 200,
    }),
  );
  await page.goto("/news");

  const reasons = page.locator(".news-grounded-assets-empty");
  await expect(reasons).toHaveCount(1);
  const contrastRatios = await reasons.evaluateAll((elements) => {
    const probe = document.createElement("span");
    document.body.append(probe);
    const parseColor = (value: string) => {
      probe.style.color = value;
      const normalized = getComputedStyle(probe).color;
      const channels = normalized
        .match(/[\d.]+/g)
        ?.slice(0, 3)
        .map(Number);
      if (!channels || channels.length !== 3) throw new Error(`Unsupported color: ${value}`);
      return channels;
    };
    const luminance = (channels: number[]) => {
      const linear = channels.map((channel) => {
        const value = channel / 255;
        return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    };
    const surface = getComputedStyle(document.documentElement).getPropertyValue("--surface-panel");
    const surfaceLuminance = luminance(parseColor(surface));
    const ratios = elements.map((element) => {
      const textLuminance = luminance(parseColor(getComputedStyle(element).color));
      const lighter = Math.max(textLuminance, surfaceLuminance);
      const darker = Math.min(textLuminance, surfaceLuminance);
      return (lighter + 0.05) / (darker + 0.05);
    });
    probe.remove();
    return ratios;
  });

  for (const ratio of contrastRatios) expect(ratio).toBeGreaterThanOrEqual(4.5);
});

test("desktop News detail keeps navigation adjacent to the event", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news/events/evt-global-policy");

  const backLink = page.getByRole("link", { name: "返回新闻事件流" });
  const hero = page.locator(".news-event-hero");
  await expect(backLink).toBeVisible();
  await expect(hero).toBeVisible();

  const backBox = await backLink.boundingBox();
  const heroBox = await hero.boundingBox();
  if (!backBox || !heroBox) throw new Error("News detail layout boxes are unavailable");

  const navigationToEventGap = heroBox.y - (backBox.y + backBox.height);
  expect(navigationToEventGap).toBeLessThanOrEqual(32);
});

test("a 668-character News feed title renders at no more than two lines", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 720 });
  await installMockApi(page);
  const longTitle = "长".repeat(668);
  await routeNewsFeed(page, [longTitle]);
  await page.goto("/news");

  const headline = page.locator(".news-event-title h2");
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

test("News card secondary actions stay interactive above the primary row link", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1366, height: 720 });
  await installMockApi(page);
  await routeNewsFeed(page, ["Readable News card with secondary evidence"]);
  await page.goto("/news");

  const originalLink = page.getByRole("link", { name: /查看原文/ });
  const rowLink = page.getByRole("link", { name: "Readable News card with secondary evidence" });

  await expect(originalLink).toBeVisible();
  await expect(rowLink).toBeVisible();
  await originalLink.click({ trial: true });
  await expect(page.locator(".news-event-triage")).toContainText("看空 · M2 · macro");
  await expect(page.locator(".news-event-delivery")).toContainText("已发送");
});

async function routeNewsFeed(page: Page, titles: string[]) {
  const feed = newsFeedFixture({
    events: titles.map((title, index) =>
      newsFeedEventFixture({
        title_zh: null,
        event_id: `news-density-${index + 1}`,
        leader_title: title,
      }),
    ),
  });
  await page.route("**/api/news/feed*", (route) =>
    route.fulfill({
      body: JSON.stringify({ data: feed, ok: true }),
      contentType: "application/json",
      status: 200,
    }),
  );
}
