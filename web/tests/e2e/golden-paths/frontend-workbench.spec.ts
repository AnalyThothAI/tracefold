import { expect, test, type Page } from "@playwright/test";
import { expectNoUnhandledApiRequests } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/*
 * `ready` has to be something that only exists once the data is in, not the static heading: the funnel card,
 * the task-tab counts and the sidebar count all arrive with the status query, and the rows with the feed
 * query. Waiting on the frame alone shot the page mid-fill and made the baselines flaky.
 */
const archetypes = [
  {
    name: "news",
    path: "/",
    ready: (page: Page) => page.locator(".news-event-row").first(),
    settled: (page: Page) => page.locator(".news-funnel-card"),
  },
  {
    name: "case",
    path: "/news/events/evt-global-policy",
    ready: (page: Page) => page.locator(".news-detail-hero"),
    settled: (page: Page) => page.locator(".news-timeline-step").first(),
  },
  {
    // #207: the deterministic OI lane. Its table is the widest thing in the console and scrolls inside
    // itself; the baseline is what keeps that from becoming a page-level horizontal scroll.
    name: "oi",
    path: "/news/oi",
    ready: (page: Page) => page.locator(".news-oi-row").first(),
    settled: (page: Page) => page.locator(".news-oi-window-row").first(),
  },
] as const;

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-07-23T10:00:00Z"));
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await installMockApi(page);
});

test("freezes representative news and case archetypes", async ({ page }) => {
  for (const route of archetypes) {
    await page.goto(route.path);
    await expect(route.ready(page)).toBeVisible();
    await expect(route.settled(page)).toBeVisible();
    await waitForSettledFeedCount(page);
    await waitForStableWorkbench(page);
    await expect(page).toHaveScreenshot(`archetype-${route.name}.png`, {
      animations: "disabled",
      caret: "hide",
      scale: "css",
      /*
       * The sticky toolbar lands on a sub-pixel boundary that moves by a fraction of a CSS pixel between
       * runs, which at tablet DPR redraws both of its rows identically but one device pixel over — ~0.0003
       * of the frame. This budget absorbs that and nothing else: a real change (a funnel tile that wraps, a
       * chip that gains a venue prefix, a row that loses its badge) is orders of magnitude larger and still
       * fails. Raise it only with a crop showing the difference is genuinely invisible.
       */
      maxDiffPixelRatio: 0.002,
    });
  }

  await expectNoUnhandledApiRequests(page);
});

/**
 * The toolbar's `visibleCount / total` grows as the anchored feed fills, and at tablet width the toolbar
 * wraps — so a shot taken while that text is still one digit wide reflows the whole second row on the next
 * render. Waiting for the count to agree with the rows on screen is what makes the tablet baseline stable.
 */
async function waitForSettledFeedCount(page: Page) {
  const total = page.locator(".news-filter-total");
  if (!(await total.count())) return;
  await expect
    .poll(async () => {
      const text = (await total.textContent()) ?? "";
      const shown = Number(text.split("/")[0]?.replace(/[^\d]/g, "") ?? "0");
      return shown === (await page.locator(".news-event-row").count());
    })
    .toBe(true);
}

async function waitForStableWorkbench(page: Page) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator(".center-column").evaluate((element) => {
    element.scrollTop = 0;
    element.scrollLeft = 0;
  });
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  await expect(page.locator("[data-page-archetype]").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Open ops diagnostics" })).toHaveCount(0);
}
