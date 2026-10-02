import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsUpdateDetailFixture, newsOutcomeFixture } from "@tests/fixtures/newsFixture";

/** Continuous reading, real scroll and keyboard links at every configured viewport. */
test("reads sent content, all facts and original without opening engineering", async ({
  page,
}, testInfo) => {
  await installMockApi(page);
  await page.goto("/news/events/evt-agent-tariff");
  await expect(
    page.getByRole("heading", { level: 1, name: "钢铁进口关税上调至 50%" }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "读者收到的推送" })).toContainText(
    "【重点】钢铁进口关税上调至 50%",
  );
  await expect(page.locator(".news-reader-fact")).toHaveCount(2);
  await expect(page.locator(".news-reader-fact").first()).toHaveAttribute("id", "news-claim-2");
  const addedFact = page.locator("#news-claim-2");
  await expect(addedFact).toContainText("报道类型：官方新表态");
  await expect(addedFact).toContainText("新增影响 2.80 / 3");
  // The synthetic fixture's status proves stored-evidence rendering, not production certification.
  await expect(addedFact).toContainText("已认证推送概率 98% · 重点概率 90%");
  await addedFact.screenshot({ path: testInfo.outputPath("reader-evidence.png") });
  await expect(page.locator("#news-processing")).not.toHaveAttribute("open", "");
  await expect(page.getByRole("tablist", { name: "事件详情" })).toHaveCount(0);
  await page.locator("#news-claim-1").getByRole("button", { name: "查看原文 1 ↗" }).click();
  await expect(page).toHaveURL(/\?focus=source-01$/);
  await expect(page.locator("#source-01")).toBeFocused();
  await page.locator("#source-01").getByRole("button", { name: "查看事实 1" }).press("Enter");
  await expect(page).toHaveURL(/\?focus=news-claim-1$/);
  await expect(page.locator("#news-claim-1")).toBeFocused();
  await page.goBack();
  await expect(page.locator("#source-01")).toBeFocused();
  await page.reload();
  await expect(page.locator("#source-01")).toBeFocused();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("opens a cold processing record and preserves comparisons during citation navigation", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/news/events/evt-agent-tariff?tab=processing&focus=delivery-record");
  await expect(page.locator("#news-processing")).toHaveAttribute("open", "");
  await expect(page.locator("#delivery-record")).toHaveAttribute("open", "");
  await expect(page.locator("#delivery-record")).toBeFocused();
  await page.locator("#delivery-record").getByText("实际发送正文").click();
  await expect(page.locator("#delivery-record")).toContainText("【重点】钢铁进口关税上调至 50%");
  await page.getByText("事实字段、来源关系与历史比较").click();
  const claim = page.locator("#claim-record-2");
  await claim.getByText("历史比较 1 项").click();
  await expect(claim.getByText(/此前：Agency announces 25% tariff/)).toBeVisible();
  await claim.getByRole("button", { name: /来源 01/ }).click();
  await expect(page.locator("#source-01")).toBeFocused();
  await page.locator("#source-01").getByRole("button", { name: "查看事实 1" }).click();
  await expect(claim.locator("#news-claim-history-2")).toHaveAttribute("open", "");
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("keeps uncertain and source-only Events readable without claiming delivery", async ({
  page,
}) => {
  await installMockApi(page);
  const detail = newsUpdateDetailFixture();
  detail.processing!.intents![0].state = "ambiguous";
  detail.processing!.intents![0].state_zh = "发送结果不明";
  detail.outcome = newsOutcomeFixture({
    group: "held",
    kind: "delivery_ambiguous",
    text_zh: "发送结果不明",
  });
  detail.deliveries = [];
  detail.reader_receipt = { state: "unknown", delivery_state: "ambiguous" };
  await page.route("**/api/news/events/evt-agent-tariff", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, data: detail }),
    }),
  );
  await page.goto("/news/events/evt-agent-tariff");
  const sent = page.getByRole("region", { name: "读者收到的推送" });
  await expect(sent).toContainText("本次没有确认送达的推送");
  await expect(sent).not.toContainText("【重点】钢铁进口关税上调至 50%");
  await expect(page.locator(".news-reader-fact[data-sent]")).toHaveCount(0);
  await expectNoDocumentHorizontalOverflow(page);
  detail.event_update = null;
  detail.processing = null;
  await page.reload();
  await expect(page.getByRole("region", { name: "每件事与推送原因" })).toContainText(
    "尚无已采用事实",
  );
  await expect(page.getByRole("region", { name: "原文" })).toContainText("Reuters World");
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("copies a shareable focused link and leaves refresh polling at the reading position", async ({
  page,
}) => {
  await installMockApi(page);
  await page.clock.install();
  let detailReads = 0;
  page.on("response", (response) => {
    if (new URL(response.url()).pathname === "/api/news/events/evt-agent-tariff") detailReads++;
  });
  await page.goto("/news/events/evt-agent-tariff?focus=news-claim-1");
  await expect(page.locator("#news-claim-1")).toBeFocused();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("button", { name: "复制事件链接" }).click();
  await expect(page.getByRole("status").filter({ hasText: "事件链接已复制" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(
    "/news/events/evt-agent-tariff?focus=news-claim-1",
  );
  await page.locator("#source-01").getByRole("link", { name: "打开原文" }).focus();
  const before = await page.locator(".center-column").evaluate((element) => element.scrollTop);
  const reads = detailReads;
  await page.clock.runFor(16_000);
  await expect.poll(() => detailReads).toBeGreaterThan(reads);
  expect(await page.locator(".center-column").evaluate((element) => element.scrollTop)).toBe(
    before,
  );
  await expect(page.locator("#source-01").getByRole("link", { name: "打开原文" })).toBeFocused();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});
