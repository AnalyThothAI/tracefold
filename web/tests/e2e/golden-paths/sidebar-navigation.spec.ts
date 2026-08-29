import { allowBrowserFailure, expect, test, type Page } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * The sidebar is fixed in the frame from 1280px up; below that, the topbar trigger opens the same navigation
 * in a drawer. Both widths reach the same four destinations from one model.
 */
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

  test("keeps all four destinations in the fixed desktop frame", async ({ page }) => {
    await installMockApi(page);
    await page.goto("/");

    const sidebarRoot = page.locator(".cockpit-app-sidebar");
    await expect(sidebarRoot).toBeVisible();
    const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
    await expect(primaryNavigation).toBeVisible();
    // The frame reserves the sidebar's width; the route column starts after it, not at the left edge.
    const panel = await sidebarRoot.boundingBox();
    expect(panel?.width ?? 0).toBeGreaterThan(180);
    const column = await page.locator(".center-column").boundingBox();
    expect(column?.x ?? 0).toBeGreaterThanOrEqual(panel?.width ?? 0);

    await expect(primaryNavigation.getByRole("link", { name: "事件流" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "资本判定" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "交易" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "OI 来源与准入审计" })).toBeVisible();
    // #256: four working surfaces in two groups, and no ReviewDesk destination at all.
    await expect(primaryNavigation.getByRole("link")).toHaveCount(4);
    await expect(primaryNavigation.getByRole("link", { name: "学习复盘" })).toHaveCount(0);
    // The mode rides beside the label without renaming the destination (#207 PR-W4).
    await expect(primaryNavigation.getByRole("link", { name: "交易" })).toContainText(
      "BINANCE_USDM_DEMO",
    );
    await expect(primaryNavigation.getByRole("link", { name: "Macro" })).toHaveCount(0);
    await expect(primaryNavigation.getByRole("link", { name: "Ops" })).toHaveCount(0);

    await expect(page.getByRole("button", { name: "切换侧栏" })).toHaveCount(0);

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });

  test("keeps the feed current on an Event drilldown", async ({ page }) => {
    await installMockApi(page);
    await page.goto("/news");

    const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
    await primaryNavigation.getByRole("link", { name: "OI 来源与准入审计" }).click();
    await expect(page).toHaveURL(/\/news\/oi$/);
    await expect(primaryNavigation.getByRole("link", { name: "OI 来源与准入审计" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await expectSidebarRouteChange(page, "事件流", "/news");
    /*
     * At this width an Event opens in the drawer beside the list (design proposal ⑦): the list stays where
     * it was and clicking another row swaps the drawer. 打开事件详情 is the canonical, shareable page,
     * and the navigation still marks the feed — and only the feed — as current on it.
     */
    await page.locator("[data-event-id] h2 a").first().click();
    await page.getByRole("dialog").getByRole("link", { name: "打开事件详情" }).click();
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
    allowBrowserFailure(page, {
      kind: "requestfailed",
      match:
        /^GET \/api\/(?:status|news\/(?:feed|status|quotes)|trading\/(?:status|orders|gate)) \(net::ERR_FAILED\)$/,
      reason:
        "This case intentionally aborts the known post-bootstrap reads to prove navigation survives.",
    });
    allowBrowserFailure(page, {
      kind: "console.error",
      match: "Failed to load resource: net::ERR_FAILED",
      reason: "Chromium reports each intentionally aborted route read in its console.",
    });
    await installMockApi(page, { failNonBootstrap: true });
    await page.goto("/news/status");

    await expectSidebarRouteChange(page, "事件流", "/news");
  });
});

test.describe("mobile bottom navigation", () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(!testInfo.project.name.startsWith("mobile-"), "mobile-only navigation contract");
  });

  test("keeps every destination under the thumb and switches routes without a drawer", async ({
    page,
  }) => {
    await installMockApi(page);
    await page.goto("/");

    // #87: no drawer to open on a phone. The bar is there from the first paint and stays there.
    await expect(page.getByRole("button", { name: "切换侧栏" })).toHaveCount(0);
    const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
    await expect(primaryNavigation).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "Radar" })).toHaveCount(0);
    await expect(primaryNavigation.getByRole("link", { name: "事件流" })).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "OI 来源与准入审计" })).toBeVisible();
    // #207: the pipeline status page kept its route and lost its slot — the topbar lamp is the way in.
    await expect(primaryNavigation.getByRole("link", { name: "流水线状态" })).toHaveCount(0);

    await primaryNavigation.getByRole("link", { name: "OI 来源与准入审计" }).click();
    await expect(page).toHaveURL(/\/news\/oi$/);
    await expect(primaryNavigation).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "OI 来源与准入审计" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await primaryNavigation.getByRole("link", { name: "事件流" }).click();
    await expect(page).toHaveURL(/\/news(?:\?|$)/);

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });
});
