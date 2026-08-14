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

test("mobile shell exposes Radar, News, and Macro navigation around rich Radar", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");
  await expectMobileTopbarContract(page);

  const sidebarTrigger = page.getByRole("button", { name: "Toggle Sidebar" });
  await sidebarTrigger.click();
  const navigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(navigation.getByRole("link", { name: "Radar" })).toBeVisible();
  await expect(navigation.getByRole("link", { name: "News" })).toBeVisible();
  await expect(navigation.getByRole("link", { name: "Macro" })).toBeVisible();
  await page.keyboard.press("Escape");

  await expect(page.getByRole("heading", { name: "Radar" })).toBeVisible();
  await expect(page.getByText("1 eligible · showing 1 / 50")).toBeVisible();
  await expect(page.getByLabel("radar window")).toHaveCount(0);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".live-radar-item"]);

  await page.getByLabel("global search").fill("test-token");
  await page.getByLabel("global search").press("Enter");
  await expect(page).toHaveURL(/\/search\?q=test-token/);
  await expectNoUnhandledApiRequests(page);
});

test("mobile Top 50 Radar remains scannable and reaches the final row", async ({ page }) => {
  await installMockApi(page, { radarItemCount: 50 });
  await page.goto("/");

  const items = page.locator(".live-radar-item");
  await expect(items).toHaveCount(50);
  await expect(items.first().getByRole("group", { name: "Price $0.042" })).toBeVisible();
  await expect(items.first().getByRole("group", { name: "Market cap $42M" })).toBeVisible();
  await expect(items.first().getByRole("img", { name: "Unpegged Token icon" })).toBeVisible();
  const layout = await page.evaluate(() => {
    const center = document.querySelector<HTMLElement>(".center-column");
    const livePage = document.querySelector<HTMLElement>(".live-radar-page");
    const queue = document.querySelector<HTMLElement>(".live-radar-queue");
    const items = document.querySelector<HTMLElement>(".live-radar-items");
    return {
      centerMaxScroll: center ? center.scrollHeight - center.clientHeight : null,
      itemsContainContents: [...document.querySelectorAll<HTMLElement>(".live-radar-item")].every(
        (item) => {
          const itemBox = item.getBoundingClientRect();
          return (
            item.scrollHeight <= item.clientHeight + 1 &&
            [...item.querySelectorAll<HTMLElement>(".live-radar-item-evidence [role=group]")].every(
              (fact) =>
                fact.scrollWidth <= fact.clientWidth + 1 &&
                fact.scrollHeight <= fact.clientHeight + 1 &&
                getComputedStyle(fact).textOverflow !== "ellipsis",
            ) &&
            [...item.children].every((child) => {
              const childBox = child.getBoundingClientRect();
              return childBox.top >= itemBox.top - 0.5 && childBox.bottom <= itemBox.bottom + 0.5;
            })
          );
        },
      ),
      livePageGridRows: livePage ? getComputedStyle(livePage).gridTemplateRows : null,
      itemsMaxScroll: items ? items.scrollHeight - items.clientHeight : null,
      queueOverflowY: queue ? getComputedStyle(queue).overflowY : null,
    };
  });
  expect(layout.centerMaxScroll).toBe(0);
  expect(layout.itemsContainContents).toBe(true);
  expect(layout.itemsMaxScroll).toBeGreaterThan(0);
  expect(layout.livePageGridRows?.trim().split(/\s+/)).toHaveLength(1);
  expect(layout.queueOverflowY).toBe("hidden");

  await expectScrollableToLastMeaningfulElement(
    page,
    ".live-radar-items",
    ".live-radar-item:last-of-type",
  );
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".live-radar-item"]);
  await expectNoUnhandledApiRequests(page);
});

test("mobile Radar Case action remains reachable on the final item", async ({ page }) => {
  await installMockApi(page, { radarItemCount: 50 });
  await page.goto("/");

  const lastItem = page.locator(".live-radar-item").last();
  await lastItem.scrollIntoViewIfNeeded();
  await lastItem.getByRole("link", { name: "Open CASE50 Token Case" }).click();
  await expect(page).toHaveURL(/focus=trigger&trigger_event_id=event-upeg-50$/);
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
