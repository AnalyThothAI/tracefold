import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * The sidebar is offcanvas and starts collapsed on every width (#70), so desktop enters the route tree through
 * the same topbar trigger as tablet and mobile.
 */
async function openSidebar(page: Page) {
  await page.getByRole("button", { name: "Toggle Sidebar" }).click();
  const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(primaryNavigation).toBeVisible();
  return primaryNavigation;
}

async function expectSidebarRouteChange(page: Page, routeName: string, expectedPath: string) {
  const primaryNavigation = await openSidebar(page);
  await Promise.all([
    page.waitForURL((url) => url.pathname === expectedPath, { timeout: 2_000 }),
    primaryNavigation.getByRole("link", { name: routeName }).click({ noWaitAfter: true }),
  ]);
}

test.describe("desktop sidebar navigation", () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(!testInfo.project.name.startsWith("desktop-"), "desktop-only sidebar contract");
  });

  test("starts collapsed and opens the single primary destination from the topbar", async ({
    page,
  }) => {
    await installMockApi(page);
    await page.goto("/");

    const sidebarRoot = page.locator('[data-slot="sidebar"]');
    await expect(sidebarRoot).toHaveAttribute("data-state", "collapsed");
    // Desktop offcanvas leaves the nav in the DOM, so assert the area it occupies rather than CSS visibility:
    // either it has no layout box at all, or it sits entirely left of the viewport.
    const navBox = await page.getByRole("navigation", { name: "Primary navigation" }).boundingBox();
    expect(navBox === null || navBox.x + navBox.width <= 0).toBe(true);
    // The point of the change: the route column starts at the left edge.
    const column = await page.locator(".center-column").boundingBox();
    expect(column?.x ?? 999).toBeLessThanOrEqual(1);

    const primaryNavigation = await openSidebar(page);
    await expect(sidebarRoot).toHaveAttribute("data-state", "expanded");
    await expect(primaryNavigation.getByRole("link", { name: "News" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link")).toHaveCount(1);
    await expect(primaryNavigation.getByRole("link", { name: "Macro" })).toHaveCount(0);
    await expect(primaryNavigation.getByRole("link", { name: "Ops" })).toHaveCount(0);

    await page.getByRole("button", { name: "Toggle Sidebar" }).click();
    await expect(sidebarRoot).toHaveAttribute("data-state", "collapsed");

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("switches desktop routes from the sidebar without waiting for route data", async ({
    page,
  }) => {
    await installMockApi(page);
    await page.goto("/news/status");

    await expectSidebarRouteChange(page, "News", "/news");

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("keeps desktop sidebar navigation instant while API requests are delayed", async ({
    page,
  }) => {
    await installMockApi(page, { delayNonBootstrapMs: 5_000 });
    await page.goto("/news/status");

    await expectSidebarRouteChange(page, "News", "/news");
  });

  test("keeps desktop sidebar navigation available when route APIs fail", async ({ page }) => {
    await installMockApi(page, { failNonBootstrap: true });
    await page.goto("/news/status");

    await expectSidebarRouteChange(page, "News", "/news");
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
    await expect(primaryNavigation.getByRole("link", { name: "News" })).toBeVisible();

    await primaryNavigation.getByRole("link", { name: "News" }).click();
    await expect(page).toHaveURL(/\/news(?:\?|$)/);
    await expect(primaryNavigation).toBeHidden();

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });
});
