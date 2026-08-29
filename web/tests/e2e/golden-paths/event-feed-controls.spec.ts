import { allowBrowserFailure, expect, test } from "@tests/e2e/fixtures";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test.setTimeout(60_000);

test("Event feed controls preserve the approved disclosure and URL contract", async ({ page }) => {
  allowBrowserFailure(page, {
    kind: "requestfailed",
    match: "GET /api/news/feed (net::ERR_ABORTED)",
    reason:
      "Changing filters intentionally supersedes an in-flight feed read; the final URL and rendered state are asserted below.",
  });
  await installMockApi(page);
  await page.goto("/news");

  const tabs = page.getByRole("tablist", { name: "按结局筛选" });
  await expect(tabs.getByRole("tab")).toHaveText(["已推送41", "被拦截271", "处理中8", "全部320"]);
  await expect(tabs.getByRole("tab", { name: "已推送 41" })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  const outcomes = [
    ["被拦截 271", "held"],
    ["处理中 8", "pending"],
    ["全部 320", "all"],
    ["已推送 41", "pushed"],
  ] as const;
  for (const [name, value] of outcomes) {
    await tabs.getByRole("tab", { name }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("outcome")).toBe(value);
    await page.reload();
    await expect(tabs.getByRole("tab", { name })).toHaveAttribute("aria-selected", "true");
  }

  const timeTrigger = page.getByRole("button", { name: "时间范围，最近 1 天" });
  await timeTrigger.focus();
  await page.keyboard.press("ArrowDown");
  let timeMenu = page.getByRole("menu", { name: "时间范围，最近 1 天" });
  await expect(timeMenu.getByRole("menuitemradio", { name: "最近 1 小时" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(timeMenu.getByRole("menuitemradio", { name: "最近 7 天" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(timeMenu).toHaveCount(0);
  await expect(timeTrigger).toBeFocused();

  const filterTrigger = page.getByRole("button", { name: "筛选" });
  await filterTrigger.click();
  const filterPanel = page.locator(".news-filter-panel");
  await expect(filterPanel).toBeVisible();
  await filterPanel.getByRole("button", { name: "▲ 利多" }).click();
  await filterPanel.getByRole("button", { name: "OI 帧" }).click();
  await expect(filterPanel.getByRole("button", { name: "▲ 利多" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(filterPanel.getByRole("button", { name: "OI 帧" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByRole("button", { name: "筛选 · 2" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);

  await page.reload();
  await expect.poll(() => new URL(page.url()).searchParams.get("direction")).toBe("bullish");
  await expect.poll(() => new URL(page.url()).searchParams.get("event_kind")).toBe("oi");
  await page.getByRole("button", { name: "筛选 · 2" }).click();
  await page.getByRole("button", { name: "清除" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("direction")).toBeNull();
  await expect.poll(() => new URL(page.url()).searchParams.get("event_kind")).toBeNull();
  await page.getByRole("button", { name: "筛选" }).click();

  const pipelineTrigger = page.getByRole("button", { name: /流水线健康/ });
  await pipelineTrigger.click();
  const pipeline = page.getByRole("dialog");
  await expect(pipeline.getByRole("listitem")).toHaveCount(5);
  for (const label of ["接入", "队列", "模型", "推送", "标的表"]) {
    await expect(pipeline.getByText(label, { exact: true })).toBeVisible();
  }
  await expect(pipeline.getByRole("link", { name: "打开流水线状态 →" })).toHaveAttribute(
    "href",
    "/news/status",
  );
  await expectNoDocumentHorizontalOverflow(page);
  await page.keyboard.press("Escape");

  await timeTrigger.click();
  await expect(filterPanel).toHaveCount(0);
  timeMenu = page.getByRole("menu", { name: "时间范围，最近 1 天" });
  await expect(timeMenu).toBeVisible();
  await timeMenu.getByRole("menuitemradio", { name: "最近 1 小时" }).click();

  await expect.poll(() => new URL(page.url()).searchParams.get("hours")).toBe("1");
  await expectNoDocumentHorizontalOverflow(page);
});
