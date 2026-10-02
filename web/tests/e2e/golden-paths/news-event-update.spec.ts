import { expect, test, type Page } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsTimelineFixture, newsUpdateDetailFixture } from "@tests/fixtures/newsFixture";

/** The detail reading flow is verified at every configured viewport, where layout and scroll exist. */
test("switches shared-card tabs without moving the reading position or losing expanded claims", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/news/events/evt-agent-tariff");

  await expect(
    page.getByRole("heading", { level: 1, name: "钢铁进口关税上调至 50%" }),
  ).toBeVisible();
  const tablist = page.getByRole("tablist", { name: "事件详情" });
  const content = page.getByRole("tabpanel", { name: "事件内容" });
  await expect(tablist.getByRole("tab")).toHaveCount(4);
  for (const name of ["事件内容", "来源证据", "当前行情", "处理记录"]) {
    await expect(tablist.getByRole("tab", { name })).toBeVisible();
  }
  await expect(page.getByRole("tabpanel")).toHaveCount(1);
  await expect(page.locator('[role="tabpanel"]')).toHaveCount(4);
  await expect(page.getByRole("navigation", { name: "事件详情目录" })).toHaveCount(0);
  await expect(content.locator(".news-update-claim")).toHaveCount(2);
  const raisedClaim = content.locator(".news-update-claim").nth(1);
  await raisedClaim.getByText("历史比较 1 项").click();
  await expect(raisedClaim.getByText(/此前：Agency announces 25% tariff/)).toBeVisible();

  await tablist.scrollIntoViewIfNeeded();
  const before = await readingPosition(page);
  await tablist.getByRole("tab", { name: "来源证据" }).click();
  await expect(page.getByRole("tabpanel", { name: "来源证据" })).toBeVisible();
  await expect(page.getByRole("tabpanel")).toHaveCount(1);
  await expect(page.getByRole("region", { name: "来源与分歧" }).getByText("反驳")).toBeVisible();
  expect(await readingPosition(page)).toEqual(before);
  await tablist.getByRole("tab", { name: "事件内容" }).click();
  await expect(raisedClaim.getByText(/此前：Agency announces 25% tariff/)).toBeVisible();
  expect(await readingPosition(page)).toEqual(before);

  const sourceButton = content.locator("#news-claim-1").getByRole("button", { name: /来源 01/ });
  await sourceButton.click();
  await expect(page).toHaveURL(/\?tab=source&focus=source-01$/);
  await expect(page.locator("#source-01")).toBeFocused();
  await page.locator("#source-01").getByRole("button", { name: "命题 01" }).click();
  await expect(page).toHaveURL(/\?tab=content&focus=news-claim-1$/);
  await expect(page.locator("#news-claim-1")).toBeFocused();
  await expect(raisedClaim.getByText(/此前：Agency announces 25% tariff/)).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("restores query reading locations on cold load, reload and back with keyboard tabs", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/news/events/evt-agent-tariff?tab=processing&focus=delivery-record");
  await expect(page.getByRole("tab", { name: "处理记录" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.locator("#delivery-record")).toHaveAttribute("open", "");
  await expect(page.locator("#delivery-record")).toBeFocused();
  await page.locator("#delivery-record").getByText("实际发送正文").click();
  await expect(page.getByText("【重点】钢铁进口关税上调至 50%", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator("#delivery-record")).toHaveAttribute("open", "");
  await expect(page.locator("#delivery-record")).toBeFocused();

  const tabs = page.getByRole("tablist", { name: "事件详情" });
  const processing = tabs.getByRole("tab", { name: "处理记录" });
  await processing.focus();
  await processing.press("Home");
  await expect(tabs.getByRole("tab", { name: "事件内容" })).toBeFocused();
  await expect(page).toHaveURL(/\?tab=content$/);
  await page.keyboard.press("ArrowRight");
  await expect(tabs.getByRole("tab", { name: "来源证据" })).toBeFocused();
  await expect(page).toHaveURL(/\?tab=source$/);
  await page.keyboard.press("End");
  await expect(processing).toBeFocused();
  await expect(page).toHaveURL(/\?tab=processing$/);
  await page.goBack();
  await expect(page).toHaveURL(/\?tab=source$/);
  await expect(page.getByRole("tabpanel", { name: "来源证据" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("tabpanel", { name: "来源证据" })).toBeVisible();
  await expect(page.getByRole("tabpanel")).toHaveCount(1);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("preserves document height and scroll when a long expanded processing panel switches to short content", async ({
  page,
}) => {
  await installMockApi(page);
  const detail = newsUpdateDetailFixture();
  const step = newsTimelineFixture()[0];
  detail.timeline = Array.from({ length: 32 }, (_, index) => ({
    ...step,
    at_ms: step.at_ms + index * 1_000,
    summary_zh: `收到已记录材料 ${index + 1}`,
  }));
  await page.route("**/api/news/events/evt-agent-tariff", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, data: detail }),
    }),
  );
  await page.goto("/news/events/evt-agent-tariff?tab=processing&focus=timeline-record");
  await expect(page.locator("#timeline-record")).toHaveAttribute("open", "");
  await expect(page.locator("#timeline-record").getByRole("listitem")).toHaveCount(32);
  await page.getByRole("tab", { name: "处理记录" }).evaluate((tab) => {
    (tab as HTMLElement).focus({ preventScroll: true });
    tab.closest(".center-column")!.scrollTop = 500;
  });
  const before = await readingPosition(page);
  expect(before.scroll).toBeGreaterThan(0);
  const slotHeight = await activeSlotHeight(page);
  await page.keyboard.press("Home");
  await expect(page.getByRole("tab", { name: "事件内容" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(await readingPosition(page)).toEqual(before);
  expect(await activeSlotHeight(page)).toBeGreaterThanOrEqual(slotHeight);
  await expect(page.getByRole("tabpanel")).toHaveCount(1);
  await page.keyboard.press("End");
  await expect(page.locator("#timeline-record")).toHaveAttribute("open", "");
  expect(await readingPosition(page)).toEqual(before);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("preserves the shell scroll position when browser back hides a manually expanded long panel", async ({
  page,
}) => {
  await installMockApi(page);
  const detail = newsUpdateDetailFixture();
  const step = newsTimelineFixture()[0];
  detail.timeline = Array.from({ length: 32 }, (_, index) => ({
    ...step,
    at_ms: step.at_ms + index * 1_000,
    summary_zh: `收到已记录材料 ${index + 1}`,
  }));
  await page.route("**/api/news/events/evt-agent-tariff", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, data: detail }),
    }),
  );
  await page.goto("/news/events/evt-agent-tariff?tab=content");
  await page.getByRole("tab", { name: "处理记录" }).click();
  await page.locator("#timeline-record > summary").click();
  await expect(page.locator("#timeline-record")).toHaveAttribute("open", "");
  await page.locator(".center-column").evaluate((shell) => {
    shell.scrollTop = 500;
  });
  const before = await readingPosition(page);
  expect(before.scroll).toBeGreaterThan(0);
  const slotHeight = await activeSlotHeight(page);
  await page.goBack();
  await expect(page.getByRole("tabpanel", { name: "事件内容" })).toBeVisible();
  expect(await readingPosition(page)).toEqual(before);
  expect(await activeSlotHeight(page)).toBeGreaterThanOrEqual(slotHeight);
  await page.goForward();
  await expect(page.getByRole("tabpanel", { name: "处理记录" })).toBeVisible();
  await expect(page.locator("#timeline-record")).toHaveAttribute("open", "");
  expect(await readingPosition(page)).toEqual(before);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

async function readingPosition(page: Page) {
  return page.getByRole("tablist", { name: "事件详情" }).evaluate((tabs) => ({
    scroll: tabs.closest(".center-column")!.scrollTop,
    top: tabs.getBoundingClientRect().top,
  }));
}

async function activeSlotHeight(page: Page) {
  return page
    .getByRole("tabpanel")
    .evaluate((panel) => panel.parentElement!.getBoundingClientRect().height);
}
