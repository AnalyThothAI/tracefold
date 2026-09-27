import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * A News Agent Event's detail (#706) at every project viewport: what is new, the claims with their own
 * mode/phase/time/conditions, the per-source relations that make a disagreement visible, the inference kept
 * apart from fact, and what the pipeline actually did -- all inside the page, with no legacy verdict panel.
 */
test("reads a News Agent Event as changes, claims, sources, inference and processing", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/news/events/evt-agent-tariff");

  await expect(
    page.getByRole("heading", { level: 1, name: "钢铁进口关税上调至 50%" }),
  ).toBeVisible();
  for (const section of ["新增了什么", "命题", "来源与分歧", "推断与缺口", "处理状态"]) {
    await expect(page.getByRole("region", { name: section })).toBeVisible();
  }
  await expect(page.getByRole("region", { name: "旧版判定" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "来源与分歧" }).getByText("反驳")).toBeVisible();
  const content = page.getByRole("region", { name: "新增了什么" });
  await expect(content.locator(".news-update-claim")).toHaveCount(2);
  const raisedClaim = content.locator(".news-update-claim").nth(1);
  await expect(raisedClaim.getByText("Agency announces 25% tariff on steel imports.")).toBeHidden();
  await raisedClaim.getByText("历史比较 1 项").click();
  await expect(
    raisedClaim.getByText("Agency announces 25% tariff on steel imports."),
  ).toBeVisible();
  await page.getByRole("navigation", { name: "事件详情目录" }).getByText("处理记录").click();
  await expect(page).toHaveURL(/#news-processing$/);
  await expect(page.getByRole("region", { name: "处理状态" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});
