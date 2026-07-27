import { expect, test, type Page } from "@playwright/test";
import { expectNoUnhandledApiRequests } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

const archetypes = [
  {
    name: "scan",
    path: "/",
    ready: (page: Page) => page.getByRole("heading", { name: "Token Radar" }),
  },
  {
    name: "case",
    path: "/search?q=HANSA&window=24h",
    ready: (page: Page) => page.getByRole("region", { name: "Token case" }),
  },
] as const;

const macroPages = [
  ["decision", "/macro", "每日宏观决策台"],
  ["research", "/macro/research", "宏观研究工作台"],
] as const;

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-07-23T10:00:00Z"));
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.routeWebSocket("**/ws", (socket) => {
    socket.onMessage((message) => {
      const payload = JSON.parse(String(message)) as { type?: string };
      if (payload.type === "auth") {
        socket.send(JSON.stringify({ type: "ready" }));
      }
    });
  });
  await installMockApi(page);
});

test("freezes representative scan and case archetypes", async ({ page }) => {
  for (const route of archetypes) {
    await page.goto(route.path);
    await expect(route.ready(page)).toBeVisible();
    await waitForStableWorkbench(page);
    await expect(page).toHaveScreenshot(`archetype-${route.name}.png`, {
      animations: "disabled",
      caret: "hide",
      scale: "css",
    });
  }

  await expectNoUnhandledApiRequests(page);
});

test("freezes the Macro decision dashboard and completed-session research workbench", async ({
  page,
}) => {
  for (const [name, path, title] of macroPages) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await waitForStableWorkbench(page);
    await expect(page).toHaveScreenshot(`macro-${name}.png`, {
      animations: "disabled",
      caret: "hide",
      scale: "css",
    });
  }

  await expectNoUnhandledApiRequests(page);
});

async function waitForStableWorkbench(page: Page) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator(".center-column").evaluate((element) => {
    element.scrollTop = 0;
    element.scrollLeft = 0;
  });
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  await expect(page.locator("[data-page-archetype]").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Open ops diagnostics" })).toHaveCount(0);
}
