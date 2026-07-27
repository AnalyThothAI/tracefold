import { expect, test } from "@playwright/test";
import { installMockApi } from "@tests/e2e/support/mockApi";

// @desktop-only-spec
test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("desktop-"), "desktop-only layout contract");
});

test("topbar keeps search and action controls contained", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 720 });
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator(".top-stats")).toHaveCount(0);
  await expect(page.locator(".searchbar")).toBeVisible();

  const layout = await page.evaluate(() => {
    const box = (selector: string) => {
      const element = document.querySelector(selector);
      if (!element) throw new Error(`Missing ${selector}`);
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        centerY: rect.top + rect.height / 2,
        width: rect.width,
      };
    };

    return {
      topbar: box(".topbar"),
      search: box(".searchbar"),
    };
  });

  expect(layout.search.width).toBeGreaterThanOrEqual(240);
  expect(layout.search.right).toBeLessThanOrEqual(layout.topbar.right);
  expect(layout.search.bottom).toBeLessThanOrEqual(layout.topbar.bottom);
  await expect(page.locator(".topbar-notification-slot")).toHaveCount(0);
});
