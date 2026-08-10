import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

async function expectSidebarRouteChange(page: Page, routeName: string, expectedPath: string) {
  const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
  await Promise.all([
    page.waitForURL((url) => url.pathname === expectedPath, { timeout: 2_000 }),
    primaryNavigation.getByRole("link", { name: routeName }).click({ noWaitAfter: true }),
  ]);
}

test.describe("desktop sidebar navigation", () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(!testInfo.project.name.startsWith("desktop-"), "desktop-only sidebar contract");
  });

  test("shows dense primary navigation and a clickable rail toggle", async ({ page }) => {
    await installMockApi(page);
    await page.goto("/");

    const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
    await expect(primaryNavigation).toBeVisible();

    for (const routeName of ["Radar", "News", "Macro"]) {
      await expect(primaryNavigation.getByRole("link", { name: routeName })).toBeVisible();
    }
    await expect(primaryNavigation.getByRole("link")).toHaveCount(3);
    await expect(primaryNavigation.getByRole("link", { name: "Ops" })).toHaveCount(0);

    const sidebarRoot = page.locator('[data-slot="sidebar"]');
    const railToggle = page.locator('[data-sidebar="rail"]');
    await expect(railToggle).toBeVisible();
    await railToggle.click();
    await expect(sidebarRoot).toHaveAttribute("data-state", "collapsed");
    await expect(railToggle).toBeVisible();
    await railToggle.click();
    await expect(sidebarRoot).toHaveAttribute("data-state", "expanded");

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("switches desktop routes from the sidebar without waiting for route data", async ({
    page,
  }) => {
    await installMockApi(page);
    await page.goto("/");

    await expectSidebarRouteChange(page, "News", "/news");
    await expectSidebarRouteChange(page, "Radar", "/");
    await expectSidebarRouteChange(page, "Macro", "/macro");

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("keeps desktop sidebar navigation instant while API requests are delayed", async ({
    page,
  }) => {
    await installMockApi(page, { delayNonBootstrapMs: 5_000 });
    await page.goto("/");

    await expectSidebarRouteChange(page, "News", "/news");
    await expectSidebarRouteChange(page, "Macro", "/macro");
  });

  test("keeps desktop sidebar navigation available when route APIs fail", async ({ page }) => {
    await installMockApi(page, { failNonBootstrap: true });
    await page.goto("/");

    await expectSidebarRouteChange(page, "News", "/news");
    await expectSidebarRouteChange(page, "Radar", "/");
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
    await expect(primaryNavigation.getByRole("link", { name: "Radar" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "News" })).toBeVisible();

    await primaryNavigation.getByRole("link", { name: "News" }).click();
    await expect(page).toHaveURL(/\/news(?:\?|$)/);
    await expect(primaryNavigation).toBeHidden();

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });
});
