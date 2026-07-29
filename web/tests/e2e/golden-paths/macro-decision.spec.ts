import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

// @responsive-spec
const modules = [
  ["/macro/rates-fed", "利率与美联储"],
  ["/macro/economy-inflation", "经济与通胀"],
  ["/macro/liquidity-funding", "流动性与融资"],
  ["/macro/credit", "信用市场"],
  ["/macro/volatility", "波动率"],
  ["/macro/cross-asset", "大类资产与期货"],
] as const;

test.beforeEach(async ({ page }) => {
  await installMockApi(page);
});

test("renders one Macro Thesis, six modules, and deterministic follow-ups", async ({ page }) => {
  await page.goto("/macro");

  await expect(page.getByRole("heading", { level: 1, name: "每日宏观主线" })).toBeVisible();
  if ((page.viewportSize()?.width ?? 0) <= 767) {
    await expect(page.getByLabel("当前宏观模块")).toBeVisible();
  } else {
    await expect(
      page.getByRole("navigation", { name: "宏观决策模块" }).getByRole("link"),
    ).toHaveCount(8);
  }
  await expect(page.getByRole("region", { name: "六个宏观模块" })).toBeVisible();
  await expect(page.getByText("十二资产：事实动量 vs 条件展望")).toBeVisible();
  await expect(page.getByText("确认主线")).toBeVisible();
  await expect(page.getByRole("link", { name: /查看主线档案/ })).toHaveAttribute(
    "href",
    "/macro/research",
  );

  await expectMacroLayout(page);
  await expectNoUnhandledApiRequests(page);
});

test("keeps the Macro Thesis inside a 390px mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/macro");

  await expect(page.getByRole("heading", { level: 1, name: "每日宏观主线" })).toBeVisible();
  await expectMacroLayout(page);
  await expectNoUnhandledApiRequests(page);
});

test("hard-loads one hash-selected category and preserves it on reload", async ({ page }) => {
  await page.goto("/macro/rates-fed#policy");

  await expect(page.getByRole("heading", { name: "政策走廊与当前市场定价" })).toBeVisible();
  await expect(page.getByText("名义 Treasury 曲线")).toHaveCount(0);
  await page.reload();
  await expect(page).toHaveURL(/\/macro\/rates-fed#policy$/);
  await expect(page.getByRole("heading", { name: "政策走廊与当前市场定价" })).toBeVisible();
  await expectNoUnhandledApiRequests(page);
});

test("keeps the chart workbench readable at 1920, 1366, 834 and 390px", async ({ page }) => {
  for (const viewport of [
    { width: 1920, height: 1080 },
    { width: 1366, height: 900 },
    { width: 834, height: 1112 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/macro/rates-fed#curve");
    await expect(page.getByRole("img", { name: "名义 Treasury 曲线" })).toBeVisible();
    await expectMacroLayout(page);
  }
  await expectNoUnhandledApiRequests(page);
});

for (const [path, title] of modules) {
  test(`hard-loads ${title} with typed facts and decision checkpoints`, async ({ page }) => {
    await page.goto(path);

    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "矛盾" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "失效条件" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "下一检查点" })).toBeVisible();
    await expect(
      page.getByText("展开 Coverage、Current Health、History Depth 与原始事实"),
    ).toBeVisible();
    await expect(page.getByRole("main", { name: title })).not.toContainText("历史窗口");

    await expectMacroLayout(page);
    await expectScrollableToLastMeaningfulElement(
      page,
      ".center-column",
      ".macro-decision__evidence",
    );
    await expectNoUnhandledApiRequests(page);
  });
}

async function expectMacroLayout(page: Page) {
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".macro-decision",
    ".macro-decision__header",
    ".macro-decision__nav",
    ".macro-decision__module-grid",
  ]);
}
