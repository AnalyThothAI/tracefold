import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test("positions lead the desk; execution opens a keyboard-dismissible Case and restores focus", async ({
  page,
}, testInfo) => {
  await installMockApi(page);
  await page.goto("/trading");
  await expect(page.getByRole("heading", { name: "交易执行" })).toBeVisible();
  const safety = page.getByLabel("执行安全状态");
  await expect(safety.getByText("执行服务在线")).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前仓位与保护" })).toBeVisible();
  for (const name of ["暂停新入场", "恢复新入场", "平掉账户仓位"]) {
    const control = page.getByRole("button", { name });
    await expect(control).toBeVisible();
    if (testInfo.project.name.startsWith("mobile-")) {
      expect((await control.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(48);
    }
  }
  const blocks = await page
    .locator("[data-block]")
    .evaluateAll((elements) => elements.map((e) => e.getAttribute("data-block")));
  expect(blocks).toEqual(["safety", "exposure", "controls", "tally"]);
  await page.getByRole("button", { name: "执行记录", exact: true }).click();
  await expect(page.locator(".trading-ledger-row")).toHaveCount(4);
  await expectNoDocumentHorizontalOverflow(page);
  const signal = page.getByRole("button", { name: "crypto:perp:BTC:USDT" });
  await signal.click();
  const drawer = page.getByRole("dialog", { name: "策略判定依据" });
  await expect(drawer).toContainText("case-btc");
  expect(new URL(page.url()).searchParams.get("case")).toBe("case-btc");
  await expectNoDocumentHorizontalOverflow(page);
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(signal).toBeFocused();
  expect(new URL(page.url()).searchParams.get("tab")).toBe("executions");
  await page.getByRole("button", { name: "策略判定", exact: true }).click();
  await expect(page.getByRole("heading", { name: "最近 24 小时 · 判定分布" })).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});
