import { allowBrowserFailure, expect, test } from "@tests/e2e/fixtures";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsFeedEventFixture, newsFeedFixture } from "@tests/fixtures/newsFixture";

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
  await filterPanel.getByRole("button", { name: "上币/下币" }).click();
  await expect(filterPanel.getByRole("button", { name: "上币/下币" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByRole("button", { name: "筛选 · 1" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);

  await page.reload();
  await expect.poll(() => new URL(page.url()).searchParams.get("event_kind")).toBe("listing");
  await page.getByRole("button", { name: "筛选 · 1" }).click();
  await page.getByRole("button", { name: "清除" }).click();
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

test("Event window refreshes one ID and stops polling after its first page is evicted", async ({
  page,
}) => {
  await installMockApi(page);
  let headline = "旧的当前标题";
  const cursors: string[] = [];
  await page.route("**/api/news/feed?**", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor") ?? "first";
    cursors.push(cursor);
    const pageNumber = cursor === "first" ? 0 : Number(cursor.slice(7));
    const template = newsFeedEventFixture();
    const event = newsFeedEventFixture({
      event_id: `evt-page-${pageNumber}`,
      update: {
        ...template.update!,
        headline: pageNumber === 0 ? headline : `第 ${pageNumber} 页事件`,
      },
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: newsFeedFixture({
          events: [event],
          next_cursor: pageNumber < 3 ? `cursor-${pageNumber + 1}` : null,
          counts: pageNumber === 0 ? newsFeedFixture().counts : null,
        }),
      }),
    });
  });

  await page.goto("/news");
  await expect(page.getByRole("heading", { name: "旧的当前标题" })).toBeVisible();
  headline = "新的当前标题";
  await expect(page.getByRole("heading", { name: "新的当前标题" })).toBeVisible({ timeout: 8_000 });
  for (let pageNumber = 1; pageNumber <= 3; pageNumber += 1) {
    await page.getByRole("button", { name: "加载更多事件" }).click();
    await expect(page.getByRole("heading", { name: `第 ${pageNumber} 页事件` })).toBeVisible();
  }
  await expect(page.getByRole("button", { name: "返回最新事件" })).toBeVisible();
  const stoppedAt = cursors.length;
  await page.waitForTimeout(3_500);
  expect(cursors).toHaveLength(stoppedAt);
  await page.getByRole("button", { name: "返回最新事件" }).click();
  await expect.poll(() => cursors.at(-1)).toBe("first");
  await expect(page.getByRole("heading", { name: "新的当前标题" })).toBeVisible();
});
