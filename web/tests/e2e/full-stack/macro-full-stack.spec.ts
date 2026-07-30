import { expect, test } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
} from "@tests/e2e/support/layoutAssertions";

test("reads the real v2 publication through FastAPI-served React", async ({ page }) => {
  const failedMacroResponses: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/macro/") && !response.ok()) {
      failedMacroResponses.push(`${response.status()} ${response.url()}`);
    }
  });

  const coldStartedAt = Date.now();
  await page.goto("/macro");
  await expect(page.getByRole("heading", { level: 1, name: "每日宏观主线" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "十二资产事实固定呈现，展望只在有传导时出现" }),
  ).toBeVisible();
  expect(Date.now() - coldStartedAt).toBeLessThanOrEqual(5_000);

  const assetTable = page.getByRole("table", { name: "十二资产冻结事实与稀疏展望" });
  await expect(assetTable.locator("[data-asset-fact]")).toHaveCount(12);
  await expect(assetTable.locator("[data-asset-fact]", { hasText: "1 周" })).toHaveCount(2);
  await expect(assetTable.getByLabel(/本次没有 material outlook/)).toHaveCount(10);
  await assetTable.focus();
  await expect(assetTable).toBeFocused();
  await assetTable.locator('[data-asset-fact="VIX"]').scrollIntoViewIfNeeded();
  await expect(assetTable.locator('[data-asset-fact="VIX"]')).toBeVisible();

  await expect(page.getByRole("navigation", { name: "宏观页面" }).getByRole("link")).toHaveCount(8);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".macro-decision",
    ".macro-decision__header",
    ".macro-decision__module-grid",
  ]);

  await page.goto("/macro/rates-fed");
  await expect(page.getByRole("heading", { level: 1, name: "利率与美联储" })).toBeVisible();
  await expect(page.locator(".macro-decision__header")).toContainText("确定性事实页");
  await expect(page.locator(".macro-decision__diagnostic-strip")).toContainText("数据合同");
  expect(
    await page.locator(".macro-decision__rates-decision").evaluate((facts) => {
      const diagnostics = document.querySelector(".macro-decision__diagnostic-strip");
      return Boolean(
        diagnostics &&
        facts.compareDocumentPosition(diagnostics) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }),
  ).toBe(true);
  await expectNoDocumentHorizontalOverflow(page);

  await page.goto("/macro/volatility");
  await expect(page.getByRole("heading", { level: 1, name: "波动率" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);

  await page.goto("/macro/research");
  await expect(page.getByRole("heading", { level: 1, name: "Macro Thesis 档案" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "证据、缺口与生成身份" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行诊断" })).toBeVisible();
  const auditDisclosure = page.getByText(/条实际引用证据/);
  await auditDisclosure.focus();
  await expect(auditDisclosure).toBeFocused();
  await expectNoDocumentHorizontalOverflow(page);

  expect(failedMacroResponses).toEqual([]);
});
