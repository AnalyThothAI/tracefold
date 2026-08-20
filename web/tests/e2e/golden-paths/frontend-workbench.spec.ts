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
    await waitForStableWorkbench(page);
    await expect(page).toHaveScreenshot(`archetype-${route.name}.png`, {
      animations: "disabled",
      caret: "hide",
      scale: "css",
    });
  }

  await expectNoUnhandledApiRequests(page);
});

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
