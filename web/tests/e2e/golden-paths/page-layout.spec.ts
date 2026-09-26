import { allowBrowserFailure, expect, test, type Page } from "@tests/e2e/fixtures";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

async function workspaceGeometry(page: Page) {
  await expect(page.locator(".page-header h1")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  return page.locator(".page-shell").evaluate((shell) => {
    const frame = document.querySelector(".center-column")!.getBoundingClientRect();
    const rect = shell.getBoundingClientRect();
    const style = getComputedStyle(shell);
    const title = shell.querySelector(".page-header h1")!;
    const heading = title.getBoundingClientRect();
    const type = getComputedStyle(title);
    return {
      left: rect.left,
      width: rect.width,
      padding: style.padding,
      gap: style.gap,
      titleLeft: heading.left,
      titleTop: heading.top - frame.top,
      titleSize: type.fontSize,
      titleLineHeight: type.lineHeight,
    };
  });
}

test("workspaces share the feed frame and title origin; documents only narrow their inner body", async ({
  page,
}, testInfo) => {
  await installMockApi(page);
  let baseline: Awaited<ReturnType<typeof workspaceGeometry>> | undefined;
  for (const path of ["/news", "/news/market", "/news/wallets", "/trading", "/news/status"]) {
    await page.goto(path);
    await expect(page.locator(".page-state-loading")).toHaveCount(0);
    const geometry = await workspaceGeometry(page);
    baseline ??= geometry;
    expect(geometry, path).toEqual(baseline);
    expect(geometry.titleSize).toBe("28px");
    expect(geometry.titleLineHeight).toBe("42px");
    const mobile = page.viewportSize()!.width < 768;
    expect(geometry.titleTop).toBe(mobile ? 12 : 16);
    expect(geometry.titleLeft - geometry.left).toBe(mobile ? 12 : 16);
    expect(geometry.width).toBeLessThanOrEqual(1340);
    await expectNoDocumentHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`${path.replaceAll("/", "-")}.png`) });
  }

  for (const path of ["/news/events/evt-global-policy", "/news/market/market-layout-proof"]) {
    await page.goto(path);
    await expect(page.locator(".page-reading-content")).toBeVisible();
    await expect(page.locator(".page-state-loading")).toHaveCount(0);
    const shell = await page.locator(".page-shell").boundingBox();
    const body = await page.locator(".page-reading-content").boundingBox();
    expect(shell!.x, path).toBe(baseline!.left);
    expect(shell!.width, path).toBe(baseline!.width);
    expect(body!.x, path).toBe(baseline!.titleLeft);
    expect(body!.width, path).toBeLessThanOrEqual(1000);
    await expectNoDocumentHorizontalOverflow(page);
  }
});

test("trading keeps its frame and title through cold load, total failure, and recovery", async ({
  page,
}) => {
  await installMockApi(page);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let recovered = false;
  allowBrowserFailure(page, {
    kind: "console.error",
    match: /503 \(Service Unavailable\)/,
    reason: "All Trading reads deliberately fail to exercise the page-level error frame.",
  });
  await page.route("**/api/trading/**", async (route) => {
    await pending;
    if (recovered) return route.fallback();
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: "trading_unavailable" }),
    });
  });
  await page.goto("/trading");
  await expect(page.getByLabel("正在读取交易台")).toBeVisible();
  const loading = await workspaceGeometry(page);
  release();
  await expect(page.locator(".page-state-error")).toBeVisible();
  expect(await workspaceGeometry(page)).toEqual(loading);
  recovered = true;
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByLabel("执行安全状态")).toBeVisible();
  expect(await workspaceGeometry(page)).toEqual(loading);
  await expectNoDocumentHorizontalOverflow(page);
});

test("an empty feed keeps the populated feed's frame and title", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news");
  await expect(page.locator(".news-event-row").first()).toBeVisible();
  const populated = await workspaceGeometry(page);
  await installMockApi(page, { emptyFeed: true });
  await page.reload();
  await expect(page.locator(".page-state-empty")).toBeVisible();
  expect(await workspaceGeometry(page)).toEqual(populated);
});
