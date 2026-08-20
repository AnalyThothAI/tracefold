import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * The sidebar is offcanvas everywhere, but it is part of the frame from 1280px up (#82) and the drawer the
 * topbar trigger opens below that (#70). Both widths reach the same two News destinations.
 */
async function openSidebar(page: Page) {
  await page.getByRole("button", { name: "Toggle Sidebar" }).click();
  const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(primaryNavigation).toBeVisible();
  return primaryNavigation;
}

async function expectSidebarRouteChange(page: Page, routeName: string, expectedPath: string) {
  const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(primaryNavigation).toBeVisible();
  await Promise.all([
    page.waitForURL((url) => url.pathname === expectedPath, { timeout: 2_000 }),
    primaryNavigation.getByRole("link", { name: routeName }).click({ noWaitAfter: true }),
  ]);
}

test.describe("desktop sidebar navigation", () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(!testInfo.project.name.startsWith("desktop-"), "desktop-only sidebar contract");
  });

  test("keeps both News destinations in the desktop frame and folds away on demand", async ({
    page,
  }) => {
    await installMockApi(page);
    await page.goto("/");

    const sidebarRoot = page.locator('[data-slot="sidebar"]');
    await expect(sidebarRoot).toHaveAttribute("data-state", "expanded");
    const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
    await expect(primaryNavigation).toBeVisible();
    // The frame reserves the sidebar's width; the route column starts after it, not at the left edge.
    // (The nav element itself is `display: contents` and has no box of its own — measure the panel.)
    const panel = await page
      .locator('[data-slot="sidebar"] [data-sidebar="sidebar"]')
      .boundingBox();
    expect(panel?.width ?? 0).toBeGreaterThan(180);
    const column = await page.locator(".center-column").boundingBox();
    expect(column?.x ?? 0).toBeGreaterThanOrEqual(panel?.width ?? 0);

    await expect(primaryNavigation.getByRole("link", { name: "事件流" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "状态" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link")).toHaveCount(2);
    await expect(primaryNavigation.getByRole("link", { name: "Macro" })).toHaveCount(0);
    await expect(primaryNavigation.getByRole("link", { name: "Ops" })).toHaveCount(0);

    // The trigger still folds it away when the reader wants the whole column.
    await page.getByRole("button", { name: "Toggle Sidebar" }).click();
    await expect(sidebarRoot).toHaveAttribute("data-state", "collapsed");
    // The offcanvas slide is animated, so poll the column's left edge rather than reading it once.
    await expect
      .poll(async () => (await page.locator(".center-column").boundingBox())?.x ?? 999)
      .toBeLessThanOrEqual(1);

    await openSidebar(page);
    await expect(sidebarRoot).toHaveAttribute("data-state", "expanded");

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("keeps the feed current on an Event drilldown", async ({ page }) => {
    await installMockApi(page);
    await page.goto("/news");

    const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
    await primaryNavigation.getByRole("link", { name: "状态" }).click();
    await expect(page).toHaveURL(/\/news\/status$/);
    await expect(primaryNavigation.getByRole("link", { name: "状态" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await expectSidebarRouteChange(page, "事件流", "/news");
    await page.locator("[data-event-id] h2 a").first().click();
    await expect(page).toHaveURL(/\/news\/events\//);
    await expect(primaryNavigation.getByRole("link", { name: "事件流" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(primaryNavigation.locator('a[aria-current="page"]')).toHaveCount(1);
  });

  test("switches desktop routes from the sidebar without waiting for route data", async ({
    page,
  }) => {
    await installMockApi(page);
    await page.goto("/news/status");

    await expectSidebarRouteChange(page, "事件流", "/news");

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("keeps desktop sidebar navigation instant while API requests are delayed", async ({
    page,
  }) => {
    await installMockApi(page, { delayNonBootstrapMs: 5_000 });
    await page.goto("/news/status");

    await expectSidebarRouteChange(page, "事件流", "/news");
  });

  test("keeps desktop sidebar navigation available when route APIs fail", async ({ page }) => {
    await installMockApi(page, { failNonBootstrap: true });
    await page.goto("/news/status");

    await expectSidebarRouteChange(page, "事件流", "/news");
  });
});

test.describe("mobile sidebar navigation", () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(!testInfo.project.name.startsWith("mobile-"), "mobile-only sidebar contract");
  });

  test("opens the route drawer and closes it after navigation", async ({ page }) => {
    await installMockApi(page);
    await page.goto("/");

    const sidebarTrigger = page.getByRole("button", { name: "Toggle Sidebar" });
    await expect(sidebarTrigger).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeHidden();

    await sidebarTrigger.click();
    const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
    await expect(primaryNavigation).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "Radar" })).toHaveCount(0);
    await expect(primaryNavigation.getByRole("link", { name: "事件流" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "状态" })).toBeVisible();

    await primaryNavigation.getByRole("link", { name: "事件流" }).click();
    await expect(page).toHaveURL(/\/news(?:\?|$)/);
    await expect(primaryNavigation).toBeHidden();

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });
});
