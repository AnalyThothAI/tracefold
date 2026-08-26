import { expect, test } from "@playwright/test";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test("Event feed controls preserve the approved disclosure and URL contract", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news");

  const tabs = page.getByRole("tablist", { name: "按结局筛选" });
  await expect(tabs.getByRole("tab")).toHaveText(["已推送41", "被拦截271", "处理中8", "全部320"]);
  await expect(tabs.getByRole("tab", { name: "已推送 41" })).toHaveAttribute(
    "aria-selected",
    "true",
  );

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

  await page.getByRole("button", { name: "最近 1 天" }).click();
  await expect(filterPanel).toHaveCount(0);
  const timeMenu = page.getByRole("menu", { name: "时间范围" });
  await expect(timeMenu).toBeVisible();
  await timeMenu.getByRole("menuitemradio", { name: "最近 1 小时" }).click();

  await expect.poll(() => new URL(page.url()).searchParams.get("direction")).toBe("bullish");
  await expect.poll(() => new URL(page.url()).searchParams.get("channel")).toBe("oi");
  await expect.poll(() => new URL(page.url()).searchParams.get("hours")).toBe("1");
  await expectNoDocumentHorizontalOverflow(page);
});
