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

test("renders one current Macro Thesis with sparse judgment and complete facts", async ({
  page,
}) => {
  await page.goto("/macro");

  await expect(page.getByRole("heading", { level: 1, name: "每日宏观主线" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "宏观页面" }).getByRole("link")).toHaveCount(8);
  await expect(
    page.getByRole("heading", { name: "真实利率回落正在缓和风险资产的贴现压力" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "尚未闭合的反证" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "本次真正重要的模块" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "十二资产事实固定呈现，展望只在有传导时出现" }),
  ).toBeVisible();
  await expect(page.locator("[data-asset-fact]")).toHaveCount(12);
  await expect(page.locator('[data-asset-fact="SPY"]')).toContainText("1 周 · 偏多");
  await expect(page.locator('[data-asset-fact="QQQ"]')).toContainText("—");
  await expect(page.getByText("正在确认").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "发布时缺口与当前事实分开看" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "仅评估 1W / 1M material outlook" }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "六个宏观模块" }).locator("article")).toHaveCount(
    6,
  );
  await expect(page.getByRole("link", { name: "研究档案" })).toHaveAttribute(
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
    { width: 1366, height: 720 },
    { width: 834, height: 1194 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/macro/rates-fed#curve");
    await expect(page.getByRole("img", { name: "名义 Treasury 曲线" })).toBeVisible();
    await expectMacroLayout(page);
  }
  await expectNoUnhandledApiRequests(page);
});

test("opens rates with the 1D tenor matrix and keeps background curves opt-in", async ({
  page,
}) => {
  await page.goto("/macro/rates-fed#curve");

  const summary = page.getByRole("region", { name: "最近完整交易日收益率决策摘要" });
  await expect(summary).toContainText(
    "最近完整交易日：2Y 下行4bp，10Y 上行6bp，30Y 上行11bp（2026-07-29）",
  );
  await expect(summary.getByRole("table", { name: "2Y 10Y 30Y 收益率矩阵" })).toBeVisible();
  await expect(
    summary.getByRole("row", { name: /2Y(?: 当前)? 4\.22% 2026-07-29(?: 1D)? -4bp/ }),
  ).toBeVisible();
  await expect(
    summary.getByRole("row", { name: /10Y(?: 当前)? 4\.67% 2026-07-29(?: 1D)? \+6bp/ }),
  ).toBeVisible();
  await expect(
    summary.getByRole("row", { name: /30Y(?: 当前)? 5\.2% 2026-07-29(?: 1D)? \+11bp/ }),
  ).toBeVisible();

  await expect(page.getByText("1周基准")).toHaveCount(0);
  await page.getByRole("checkbox", { name: "1W" }).check();
  await expect(page.getByText("1周基准 · 2026-07-22")).toBeVisible();
  await expectNoUnhandledApiRequests(page);
});

for (const [path, title] of modules) {
  test(`hard-loads ${title} with typed facts and decision checkpoints`, async ({ page }) => {
    await page.goto(path);

    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "宏观页面" })).toBeVisible();
    await expect(page.getByRole("complementary", { name: "图表决策注释" })).toBeVisible();
    await expect(page.locator(".macro-decision__header")).toContainText("数据合同");
    await expect(
      page.getByText(
        path === "/macro/rates-fed"
          ? "期限、窗口和来源均由持久化合同提供"
          : "只展示当前 API 返回的判断与检查项",
      ),
    ).toBeVisible();
    await expect(page.locator(".macro-decision")).not.toContainText("历史窗口");
    await expect(page.locator(".macro-decision")).not.toContainText(
      "展开 Coverage、Current Health、History Depth 与原始事实",
    );

    await expectMacroLayout(page);
    await expectScrollableToLastMeaningfulElement(
      page,
      ".center-column",
      ".macro-decision__semantic-section",
    );
    await expectNoUnhandledApiRequests(page);
  });
}

async function expectMacroLayout(page: Page) {
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".macro-decision",
    ".macro-decision__header",
    { selector: ".macro-decision__nav", allowHorizontalOverflow: true },
    { selector: ".macro-thesis-report__asset-table", allowHorizontalOverflow: true },
    { selector: ".macro-thesis-report__recovery", allowHorizontalOverflow: true },
  ]);
}
