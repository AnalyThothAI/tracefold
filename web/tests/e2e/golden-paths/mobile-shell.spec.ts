import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("mobile-"), "mobile-only layout contract");
});

test("mobile shell exposes sidebar route nav and keeps Radar task-local", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await expectMobileTopbarContract(page);

  const sidebarTrigger = page.getByRole("button", { name: "Toggle Sidebar" });
  await expect(sidebarTrigger).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeHidden();

  await sidebarTrigger.click();
  const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(primaryNavigation).toBeVisible();
  await expect(primaryNavigation.getByRole("link", { name: "Radar" })).toBeVisible();
  await expect(primaryNavigation.getByRole("link", { name: "Stocks" })).toBeVisible();
  await expect(primaryNavigation.getByRole("link", { name: "Macro" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(primaryNavigation).toBeHidden();

  await expect(page.locator(".live-task-nav")).toHaveCount(0);
  await expect(page.getByText(/实时信号 Tape/i)).toHaveCount(0);
  await expect(page.getByTestId("radar-content-status")).toBeVisible();
  await expect(page.getByTestId("radar-content-status")).toContainText(/最新内容 \d/);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".radar-toolbar"]);

  await page.getByLabel("global search").fill("test-token");
  await page.getByLabel("global search").press("Enter");
  await expect(page).toHaveURL(/\/search\?q=test-token/);
  await expectNoUnhandledApiRequests(page);
});

test("mobile radar list remains reachable without reserved task-nav space", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/?window=24h");

  await expect(page.getByRole("button", { name: "Toggle Sidebar" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeHidden();
  await expect(page.locator(".live-task-nav")).toHaveCount(0);
  await expect(page.locator(".token-radar-row")).toHaveCount(8);

  const layout = await page.evaluate(() => {
    const center = document.querySelector<HTMLElement>(".center-column");
    const livePage = document.querySelector<HTMLElement>(".live-page");
    const radarPanel = document.querySelector<HTMLElement>(".radar-panel");
    const tokenTable = document.querySelector<HTMLElement>(".token-radar-table");
    return {
      centerMaxScroll: center ? center.scrollHeight - center.clientHeight : null,
      livePageGridRows: livePage ? getComputedStyle(livePage).gridTemplateRows : null,
      tokenTableMaxScroll: tokenTable ? tokenTable.scrollHeight - tokenTable.clientHeight : null,
      radarPanelOverflowY: radarPanel ? getComputedStyle(radarPanel).overflowY : null,
    };
  });

  expect(layout.centerMaxScroll).toBe(0);
  expect(layout.tokenTableMaxScroll).toBeGreaterThan(0);
  expect(layout.livePageGridRows?.trim().split(/\s+/)).toHaveLength(1);
  expect(layout.radarPanelOverflowY).toBe("hidden");

  await expectScrollableToLastMeaningfulElement(
    page,
    ".token-radar-table",
    ".token-radar-row:last-of-type",
  );
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".token-radar-row"]);
  await expectNoUnhandledApiRequests(page);
});

test("mobile radar row click reaches token detail without task-nav interception", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/?window=24h");

  const rows = page.locator(".token-radar-row");
  await expect(rows).toHaveCount(8);
  const lastRow = rows.last();
  await lastRow.scrollIntoViewIfNeeded();

  const hitTest = await lastRow.evaluate((row) => {
    const rect = row.getBoundingClientRect();
    const element = document.elementFromPoint(
      rect.left + rect.width / 2,
      rect.top + rect.height / 2,
    );
    return {
      rowContainsHit: element ? row.contains(element) : false,
      hitClassName: element instanceof HTMLElement ? element.className : "",
      hitTagName: element?.tagName ?? "",
    };
  });
  expect(hitTest).toMatchObject({
    rowContainsHit: true,
  });

  await lastRow.click();
  await expect(page).toHaveURL(/\/token\/Asset\/asset%3Adex%3Aeth%3A/);
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
    ["search input", page.getByLabel("global search")],
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

declare global {
  interface Window {
    __routeBackSentinel?: string;
  }
}
