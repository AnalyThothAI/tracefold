import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("mobile-"), "mobile-only layout contract");
});

test("mobile shell exposes News navigation around the News landing", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/news(?:\?|$)/);
  await expectMobileTopbarContract(page);

  /*
   * #87: navigation on a phone is a fixed bottom bar, not a drawer. Every destination is visible without a
   * tap, and there is no sidebar trigger left to press.
   */
  await expect(page.getByRole("button", { name: "Toggle Sidebar" })).toHaveCount(0);
  const navigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(navigation).toBeVisible();
  await expect(navigation.getByRole("link", { name: "事件流" })).toBeVisible();
  await expect(navigation.getByRole("link", { name: "OI 遥测审计" })).toBeVisible();
  // #207: 流水线状态 kept its route and lost its slot; the topbar lamp is the way in, on every frame.
  await expect(navigation.getByRole("link", { name: "流水线状态" })).toHaveCount(0);
  await expect(navigation.getByRole("link", { name: "Radar" })).toHaveCount(0);
  await expect(navigation.getByRole("link", { name: "Macro" })).toHaveCount(0);
  await expectBottomNavAboveTheFold(page);

  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar"]);

  await page.getByLabel("news search").fill("test-token");
  await page.getByLabel("news search").press("Enter");
  await expect(page).toHaveURL(/\/news\?q=test-token/);
  await expectNoUnhandledApiRequests(page);
});

async function expectMobileTopbarContract(page: Page) {
  const topbar = page.locator(".topbar");
  const centerColumn = page.locator(".center-column");
  await expect(topbar).toBeVisible();
  await expect(centerColumn).toBeVisible();

  const [topbarRect, centerColumnRect, mobileTopbarHeightToken] = await Promise.all([
    locatorRect(topbar, ".topbar"),
    locatorRect(centerColumn, ".center-column"),
    topbar.evaluate((element) =>
      getComputedStyle(element.ownerDocument.documentElement)
        .getPropertyValue("--shell-mobile-topbar-height")
        .trim(),
    ),
  ]);
  expect(mobileTopbarHeightToken).toBe("52px");
  expect(topbarRect.height).toBeCloseTo(52, 0);
  expect(topbarRect.bottom).toBeLessThanOrEqual(centerColumnRect.top + 0.5);

  const search = page.getByLabel("news search");
  await expect(search, "search input should render in the mobile topbar").toBeVisible();
  expectRectContained(await locatorRect(search, "search input"), topbarRect, "search input");
  await expect(page.getByRole("button", { name: "刷新" })).toHaveCount(0);
}

/**
 * The bar has to sit inside the viewport with the scroller stopping above it — a bar that overlaps the last
 * Event, or one pushed below the fold, both read as "the list just ends" (#87).
 */
async function expectBottomNavAboveTheFold(page: Page) {
  const nav = page.locator(".cockpit-bottom-nav");
  const [navRect, columnRect, viewport] = await Promise.all([
    locatorRect(nav, ".cockpit-bottom-nav"),
    locatorRect(page.locator(".center-column"), ".center-column"),
    page.viewportSize(),
  ]);
  expect(navRect.bottom).toBeLessThanOrEqual((viewport?.height ?? 0) + 0.5);
  expect(columnRect.bottom).toBeLessThanOrEqual(navRect.top + 0.5);
  // Every tab is a thumb target, and the bottom edge of a screen is where aim is worst.
  for (const link of await nav.getByRole("link").all()) {
    const rect = await locatorRect(link, "bottom nav link");
    expect(rect.height).toBeGreaterThanOrEqual(44);
  }
}

type Rect = {
  bottom: number;
  height: number;
  left: number;
  right: number;
  top: number;
  width: number;
};

async function locatorRect(locator: Locator, name: string): Promise<Rect> {
  const box = await locator.boundingBox();
  expect(box, `${name} should have a layout box`).not.toBeNull();
  return {
    bottom: box!.y + box!.height,
    height: box!.height,
    left: box!.x,
    right: box!.x + box!.width,
    top: box!.y,
    width: box!.width,
  };
}

function expectRectContained(rect: Rect, container: Rect, name: string) {
  expect(rect.top, `${name} top should fit inside .topbar`).toBeGreaterThanOrEqual(
    container.top - 0.5,
  );
  expect(rect.bottom, `${name} bottom should fit inside .topbar`).toBeLessThanOrEqual(
    container.bottom + 0.5,
  );
  expect(rect.left, `${name} left should fit inside .topbar`).toBeGreaterThanOrEqual(
    container.left - 0.5,
  );
  expect(rect.right, `${name} right should fit inside .topbar`).toBeLessThanOrEqual(
    container.right + 0.5,
  );
}
