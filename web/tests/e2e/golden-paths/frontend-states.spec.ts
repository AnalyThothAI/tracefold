import { allowBrowserFailure, expect, test } from "@tests/e2e/fixtures";
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
    if (state.options.failNonBootstrap) {
      allowBrowserFailure(page, {
        kind: "requestfailed",
        match:
          /^GET \/api\/(?:status|news\/(?:feed|status|quotes)|trading\/(?:status|orders|gate)) \(net::ERR_FAILED\)$/,
        reason:
          "This case intentionally aborts the known post-bootstrap reads to prove network failure UI.",
      });
      allowBrowserFailure(page, {
        kind: "console.error",
        match: "Failed to load resource: net::ERR_FAILED",
        reason: "Chromium reports each intentionally aborted post-bootstrap read in its console.",
      });
    }
    if (state.options.bootstrapStatus === 401) {
      allowBrowserFailure(page, {
        kind: "console.error",
        match: "Failed to load resource: the server responded with a status of 401 (Unauthorized)",
        reason: "This case intentionally proves the bootstrap unauthorized page state.",
      });
    }
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
      // Chromium can rasterize the tabular toolbar counts by a few anti-aliased edge pixels when the four
      // projects run concurrently. The budget covers those glyph edges only; any control movement is orders
      // of magnitude larger.
      maxDiffPixels: 100,
      scale: "css",
    });
  });
}
