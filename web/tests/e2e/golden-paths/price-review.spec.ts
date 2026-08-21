import { expect, test } from "@playwright/test";
import { expectNoUnhandledApiRequests } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * 学习复盘 is reachable at every breakpoint, hard-loadable at its own URL, and carries its window in the
 * URL so an evidence queue can be shared.
 *
 * The two market meanings must stay visibly apart on the Event surfaces — a current quote beside a chip, a
 * fixed post-Event return in its own block — because a rolling 24 h change read as an Event's reaction is
 * the misreading this whole plane exists to prevent.
 *
 * @responsive-spec — the destination has to be reachable under the thumb as well as in the sidebar, so this
 * spec drives the desktop and phone frames explicitly rather than trusting one project's default viewport.
 */
test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-07-23T10:00:00Z"));
  await installMockApi(page);
});

test("reaches 学习复盘 from the sidebar and keeps its window in the URL", async ({ page }) => {
  await page.setViewportSize({ height: 720, width: 1366 });
  await page.goto("/news");

  await page.getByRole("link", { name: "学习复盘" }).click();
  await expect(page).toHaveURL(/\/news\/review$/);
  await expect(page.getByRole("heading", { level: 1, name: "学习复盘" })).toBeVisible();

  await expect(page.getByText("待复盘事件")).toBeVisible();
  await expect(page.getByText("外部漏召回")).toBeVisible();

  await page.getByRole("combobox").selectOption("720");
  await expect(page).toHaveURL(/hours=720/);
  await expectNoUnhandledApiRequests(page);
});

test("hard-loads 学习复盘 on a phone and keeps the bottom navigation reachable", async ({
  page,
}) => {
  await page.setViewportSize({ height: 844, width: 390 });
  await page.goto("/news/review");

  await expect(page.getByRole("heading", { level: 1, name: "学习复盘" })).toBeVisible();
  const bottomNav = page.getByRole("navigation", { name: "Primary navigation" }).last();
  await expect(bottomNav.getByRole("link", { name: "学习复盘" })).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await expectNoUnhandledApiRequests(page);
});

test("keeps the current quote and the post-Event reaction visibly apart on an Event", async ({
  page,
}) => {
  await page.setViewportSize({ height: 900, width: 1440 });
  await page.goto("/news/events/evt-global-policy");

  /*
   * Two market blocks, never one table. The current price sits with the assets it belongs to, inside the
   * hero; the fixed post-Event return is a card of its own further down. A rolling 24H change and a return
   * anchored at this headline are different time semantics, and a single table would invite reading the
   * first as the market's answer to the second.
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
