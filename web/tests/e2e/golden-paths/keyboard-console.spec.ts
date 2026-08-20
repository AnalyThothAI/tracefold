import { expect, test } from "@playwright/test";
import { expectNoUnhandledApiRequests } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * The console's keyboard contract (#82). The bindings are advertised by the `?` panel and implemented
 * between the shell (search, go-to, panel, leaving an Event) and the feed (the reading cursor, task tabs).
 */
test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("desktop-"), "desktop-only keyboard contract");
});

const cursorRow = ".news-event-row[data-cursor]";

test("moves a reading cursor down the feed and opens the Event under it", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news");
  await expect(page.locator(".news-event-row").first()).toBeVisible();
  await expect(page.locator(cursorRow)).toHaveCount(0);

  await page.keyboard.press("j");
  await expect(page.locator(cursorRow)).toHaveAttribute("data-event-id", "evt-global-policy");
  await page.keyboard.press("j");
  await expect(page.locator(cursorRow)).toHaveAttribute("data-event-id", "evt-global-policy-2");
  await page.keyboard.press("k");
  await expect(page.locator(cursorRow)).toHaveAttribute("data-event-id", "evt-global-policy");

  // The cursor is real focus, so Enter is the row's own activation rather than a synthetic one.
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/news\/events\/evt-global-policy$/);
  await expect(page.getByRole("region", { name: "新闻事件详情" })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(/\/news(\?|$)/);

  await expectNoUnhandledApiRequests(page);
});

test("selects task tabs with the digits the toolbar advertises", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news");
  await expect(page.getByRole("tab", { name: "全部" })).toHaveAttribute("aria-selected", "true");

  await page.keyboard.press("2");
  await expect(page).toHaveURL(/outcome=pushed/);
  await expect(page.getByRole("tab", { name: "已推送" })).toHaveAttribute("aria-selected", "true");

  await page.keyboard.press("3");
  await expect(page).toHaveURL(/outcome=held/);

  await page.keyboard.press("1");
  await expect(page).not.toHaveURL(/outcome=/);
  await expect(page.getByRole("tab", { name: "全部" })).toHaveAttribute("aria-selected", "true");
});

test("routes with the go-to prefix, focuses search, and never hijacks a typed key", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/news");
  await expect(page.locator(".news-event-row").first()).toBeVisible();

  await page.keyboard.press("g");
  await page.keyboard.press("s");
  await expect(page).toHaveURL(/\/news\/status$/);
  await page.keyboard.press("g");
  await page.keyboard.press("f");
  await expect(page).toHaveURL(/\/news(\?|$)/);

  await page.keyboard.press("/");
  const search = page.getByLabel("news search");
  await expect(search).toBeFocused();

  // Typing in the search box must reach the box, not the feed's cursor or the go-to prefix.
  await page.keyboard.type("gj2");
  await expect(search).toHaveValue("gj2");
  await expect(page.locator(cursorRow)).toHaveCount(0);
  await expect(page).not.toHaveURL(/outcome=/);

  // Escape leaves the field without leaving the route.
  await page.keyboard.press("Escape");
  await expect(search).not.toBeFocused();
  await expect(page).toHaveURL(/\/news(\?|$)/);
});

test("opens and closes the shortcut panel", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news");
  await expect(page.locator(".news-event-row").first()).toBeVisible();

  await page.keyboard.press("?");
  const panel = page.getByRole("dialog", { name: "快捷键" });
  await expect(panel).toBeVisible();
  await expect(panel.getByText("上一条 / 下一条")).toBeVisible();
  await expect(panel.getByText("复制「判错了」标注命令")).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(page).toHaveURL(/\/news(\?|$)/);
});

test("copies the label command for the cursor row and says so", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await installMockApi(page);
  await page.goto("/news");
  await expect(page.locator(".news-event-row").first()).toBeVisible();

  await page.keyboard.press("j");
  await page.keyboard.press("x");

  await expect(page.getByRole("status")).toHaveText("已复制「判错了」标注命令");
  // The console hands over the CLI rather than writing the label: the News API is read-only.
  const clipboard = await page.evaluate(() => navigator.clipboard.readText());
  expect(clipboard).toBe("tracefold news label evt-global-policy --label bad");
  await expectNoUnhandledApiRequests(page);
});
