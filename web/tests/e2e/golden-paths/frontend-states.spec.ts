import { expect, test } from "@playwright/test";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi, type MockApiOptions } from "@tests/e2e/support/mockApi";

const states: Array<{
  name: string;
  options: MockApiOptions;
  ready: (page: import("@playwright/test").Page) => import("@playwright/test").Locator;
}> = [
  {
    name: "loading",
    options: { delayNonBootstrapMs: 5_000 },
    ready: (page) => page.locator(".page-state-loading"),
  },
  {
    name: "empty",
    options: { emptyFeed: true },
    ready: (page) => page.locator(".page-state-empty"),
  },
  {
    name: "error",
    options: { failNonBootstrap: true },
    ready: (page) => page.locator(".page-state-error"),
  },
  {
    name: "unauthorized",
    options: { bootstrapStatus: 401 },
    ready: (page) => page.locator(".page-state-error"),
  },
];

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-07-23T10:00:00Z"));
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
});

for (const state of states) {
  test(`freezes the shared ${state.name} route state`, async ({ page }) => {
    await installMockApi(page, state.options);
    await page.goto("/news");
    await expect(state.ready(page)).toBeVisible();
    await page.evaluate(async () => {
      await document.fonts.ready;
    });
    await expectNoDocumentHorizontalOverflow(page);
    await expect(page).toHaveScreenshot(`state-${state.name}.png`, {
      animations: "disabled",
      caret: "hide",
      scale: "css",
    });
  });
}
