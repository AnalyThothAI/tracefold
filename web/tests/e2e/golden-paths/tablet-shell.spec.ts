import { expect, test } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("tablet-"), "tablet-only shell contract");
});

test("tablet shell keeps top-level route navigation in the sidebar drawer", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator(".live-task-nav")).toHaveCount(0);
  await expect(page).toHaveURL(/\/news(?:\?|$)/);
  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeVisible();

  const sidebarTrigger = page.getByRole("button", { name: "Toggle Sidebar" });
  await expect(sidebarTrigger).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeHidden();

  await sidebarTrigger.click();
  const primaryNavigation = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(primaryNavigation).toBeVisible();
  for (const routeName of ["News", "Macro"]) {
    await expect(primaryNavigation.getByRole("link", { name: routeName })).toBeVisible();
  }
  await expect(primaryNavigation.getByRole("link")).toHaveCount(2);

  await primaryNavigation.getByRole("link", { name: "Macro" }).click();
  await expect(page).toHaveURL(/\/macro(?:\?|$)/);
  await expect(primaryNavigation).toBeHidden();
  await expect(page.getByRole("heading", { level: 1, name: "宏观事实总览" })).toBeVisible();

  await expect(sidebarTrigger).toBeVisible();
  await sidebarTrigger.click();
  await expect(primaryNavigation).toBeVisible();
  await primaryNavigation.getByRole("link", { name: "News" }).click();
  await expect(page).toHaveURL(/\/news(?:\?|$)/);
  await expect(primaryNavigation).toBeHidden();

  await page.getByLabel("news search").fill("tablet-token");
  await page.getByRole("button", { name: "检索" }).click();
  await expect(page).toHaveURL(/\/news\?q=tablet-token/);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});
