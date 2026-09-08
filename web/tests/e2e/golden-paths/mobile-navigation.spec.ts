import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * The phone half of the one navigation model. It shared a file with the desktop sidebar until #598
 * D8: one file cannot be partitioned by `playwright.config.ts`'s per-project `testMatch`, and the
 * runtime `test.skip` that used to separate the two halves is exactly what a required Playwright
 * report may not contain.
 */
test.describe("mobile bottom navigation", () => {
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
    await expect(primaryNavigation.getByRole("link", { name: "市场研究" })).toBeVisible();
    // #207: the pipeline status page kept its route and lost its slot — the topbar lamp is the way in.
    await expect(primaryNavigation.getByRole("link", { name: "流水线状态" })).toHaveCount(0);

    await primaryNavigation.getByRole("link", { name: "市场研究" }).click();
    await expect(page).toHaveURL(/\/news\/market$/);
    await expect(primaryNavigation).toBeVisible();
    await expect(primaryNavigation.getByRole("link", { name: "市场研究" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await primaryNavigation.getByRole("link", { name: "事件流" }).click();
    await expect(page).toHaveURL(/\/news(?:\?|$)/);

    await expectNoDocumentHorizontalOverflow(page);
    await expectNoUnhandledApiRequests(page);
  });
});
