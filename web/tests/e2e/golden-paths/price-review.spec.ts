import { expect, test } from "@playwright/test";
import { expectNoUnhandledApiRequests } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * 命中复盘 as a real destination (#88): reachable from the navigation at every breakpoint, hard-loadable at
 * its own URL, and carrying its window in the URL so a view can be shared.
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

test("reaches 命中复盘 from the sidebar and keeps its window in the URL", async ({ page }) => {
  await page.setViewportSize({ height: 900, width: 1440 });
  await page.goto("/news");

  await page.getByRole("link", { name: "命中复盘" }).click();
  await expect(page).toHaveURL(/\/news\/review$/);
  await expect(page.getByRole("heading", { level: 1, name: "命中复盘" })).toBeVisible();

  // Coverage first: the denominators are on screen before any accuracy figure.
  await expect(page.getByLabel("复盘覆盖率")).toBeVisible();
  await expect(page.getByLabel("方向命中率")).toBeVisible();

  await page.getByRole("combobox").selectOption("720");
  await expect(page).toHaveURL(/hours=720/);
  await expectNoUnhandledApiRequests(page);
});

test("hard-loads 命中复盘 on a phone and keeps the bottom navigation reachable", async ({
  page,
}) => {
  await page.setViewportSize({ height: 780, width: 390 });
  await page.goto("/news/review");

  await expect(page.getByRole("heading", { level: 1, name: "命中复盘" })).toBeVisible();
  const bottomNav = page.getByRole("navigation", { name: "Primary navigation" }).last();
  await expect(bottomNav.getByRole("link", { name: "命中复盘" })).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await expectNoUnhandledApiRequests(page);
});

test("keeps the current quote and the post-Event reaction visibly apart on an Event", async ({
  page,
}) => {
  await page.setViewportSize({ height: 900, width: 1440 });
  await page.goto("/news/events/evt-global-policy");

  await expect(page.getByLabel("当前报价")).toBeVisible();
  await expect(page.getByLabel("事件后反应")).toBeVisible();
  // The current price names its venue and its price kind; the reaction names its horizon.
  await expect(page.getByLabel("当前报价").getByText("最新成交价").first()).toBeVisible();
  await expectNoUnhandledApiRequests(page);
});

test("the topbar hit figure never appears without its denominator", async ({ page }) => {
  await page.setViewportSize({ height: 900, width: 1440 });
  await page.goto("/news");

  const figure = page.locator(".topbar-figures").getByText("HIT 1H").locator("..");
  await expect(figure).toContainText("N=");
});
