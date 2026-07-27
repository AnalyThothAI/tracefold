import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

async function expectSidebarRouteClickFast(
  page: Page,
  routeName: string,
  expectedPath: string,
  budgetMs = 500,
) {
  const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
  const startedAt = Date.now();

  await Promise.all([
    page.waitForURL((url) => url.pathname === expectedPath),
    primaryNavigation.getByRole("link", { name: routeName }).click(),
  ]);

  expect(Date.now() - startedAt).toBeLessThanOrEqual(budgetMs);
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

    for (const routeName of ["Radar", "Stocks", "News", "Macro"]) {
      await expect(primaryNavigation.getByRole("link", { name: routeName })).toBeVisible();
    }
    await expect(primaryNavigation.getByRole("link")).toHaveCount(4);
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

    await expectSidebarRouteClickFast(page, "News", "/news");
    await expectSidebarRouteClickFast(page, "Stocks", "/stocks");
    await expectSidebarRouteClickFast(page, "Radar", "/");
    await expectSidebarRouteClickFast(page, "Macro", "/macro");

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("keeps desktop sidebar navigation instant while API requests are delayed", async ({
    page,
  }) => {
    await installMockApi(page, { delayNonBootstrapMs: 5_000 });
    await page.goto("/");

    await expectSidebarRouteClickFast(page, "News", "/news");
    await expectSidebarRouteClickFast(page, "Stocks", "/stocks");
  });

  test("keeps desktop sidebar navigation available when route APIs fail", async ({ page }) => {
    await installMockApi(page, { failNonBootstrap: true });
    await page.goto("/");

    await expectSidebarRouteClickFast(page, "News", "/news");
    await expectSidebarRouteClickFast(page, "Radar", "/");
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
