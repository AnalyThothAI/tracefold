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

  const sidebarTrigger = page.getByRole("button", { name: "Toggle Sidebar" });
  await sidebarTrigger.click();
  const navigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(navigation.getByRole("link", { name: "Radar" })).toHaveCount(0);
  await expect(navigation.getByRole("link", { name: "News" })).toBeVisible();
  await expect(navigation.getByRole("link", { name: "Macro" })).toHaveCount(0);
  await page.keyboard.press("Escape");

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
  expect(mobileTopbarHeightToken).toBe("50px");
  expect(topbarRect.height).toBeCloseTo(50, 0);
  expect(topbarRect.bottom).toBeLessThanOrEqual(centerColumnRect.top + 0.5);

  for (const [name, locator] of [
    ["sidebar trigger", page.getByRole("button", { name: "Toggle Sidebar" })],
    ["search input", page.getByLabel("news search")],
  ] satisfies Array<[string, Locator]>) {
    await expect(locator, `${name} should render in the mobile topbar`).toBeVisible();
    expectRectContained(await locatorRect(locator, name), topbarRect, name);
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
