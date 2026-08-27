import { expect, test } from "@tests/e2e/fixtures";
import { expectNoUnhandledApiRequests } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

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
