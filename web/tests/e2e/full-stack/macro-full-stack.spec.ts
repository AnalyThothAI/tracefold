import { expect, test } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
} from "@tests/e2e/support/layoutAssertions";

test("reads current Macro facts through FastAPI-served React", async ({ page }) => {
  const failedMacroResponses: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/macro/") && !response.ok()) {
      failedMacroResponses.push(`${response.status()} ${response.url()}`);
    }
  });

  const coldStartedAt = Date.now();
  await page.goto("/macro");
  await expect(page.getByRole("heading", { level: 1, name: "宏观事实总览" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前事实摘要" })).toBeVisible();
  expect(Date.now() - coldStartedAt).toBeLessThanOrEqual(5_000);

  await expect(page.getByRole("region", { name: "六个宏观模块" }).locator("article")).toHaveCount(
    6,
  );
  await expect(page.getByRole("navigation", { name: "宏观页面" }).getByRole("link")).toHaveCount(7);
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

  expect(failedMacroResponses).toEqual([]);
});
