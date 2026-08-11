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

const moduleViews = [
  ["/macro/rates-fed", "curve", "收益率曲线"],
  ["/macro/rates-fed", "policy", "政策走廊"],
  ["/macro/rates-fed", "fed", "美联储沟通"],
  ["/macro/rates-fed", "positioning", "利率仓位"],
  ["/macro/economy-inflation", "inflation", "通胀"],
  ["/macro/economy-inflation", "labor", "就业"],
  ["/macro/economy-inflation", "growth", "增长"],
  ["/macro/liquidity-funding", "balance-sheet", "资产负债表"],
  ["/macro/liquidity-funding", "funding", "融资条件"],
  ["/macro/credit", "cycle", "周期四维"],
  ["/macro/credit", "spreads", "评级利差"],
  ["/macro/credit", "funding", "融资成本"],
  ["/macro/credit", "banks", "银行供需"],
  ["/macro/credit", "quality", "贷款质量"],
  ["/macro/credit", "confirmation", "市场确认"],
  ["/macro/volatility", "term", "现货–3M"],
  ["/macro/volatility", "cross-asset", "跨资产隐波"],
  ["/macro/cross-asset", "returns", "收益矩阵"],
  ["/macro/cross-asset", "normalized", "分组走势"],
  ["/macro/cross-asset", "correlations", "相关矩阵"],
  ["/macro/cross-asset", "futures", "期货与仓位"],
] as const;

test.beforeEach(async ({ page }) => {
  await installMockApi(page);
});

test("renders the six current Macro fact modules", async ({ page }) => {
  await page.goto("/macro");

  await expect(page.getByRole("heading", { level: 1, name: "宏观事实总览" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前事实摘要" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "宏观页面" }).getByRole("link")).toHaveCount(7);
  await expect(page.getByRole("region", { name: "六个宏观模块" }).locator("article")).toHaveCount(
    6,
  );

  await expectMacroLayout(page);
  await expectNoUnhandledApiRequests(page);
});

test("keeps current Macro facts inside a 390px mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/macro");

  await expect(page.getByRole("heading", { level: 1, name: "宏观事实总览" })).toBeVisible();
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

test("hard-loads all 21 internal hashes as one reachable workbench at every viewport", async ({
  page,
}) => {
  test.slow();
  expect(moduleViews).toHaveLength(21);
  for (const [path, hash, label] of moduleViews) {
    await page.goto(`${path}#${hash}`);

    await expect(page).toHaveURL(new RegExp(`${path}#${hash}$`));
    if ((page.viewportSize()?.width ?? 0) <= 767) {
      await expect(page.locator(".macro-decision__section-select select")).toHaveValue(hash);
    } else {
      await expect(page.getByRole("tab", { name: label })).toHaveAttribute("aria-selected", "true");
    }
    const panel = page.getByRole("tabpanel", { name: label });
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("heading", { level: 2 })).toHaveCount(1);
    await expect(page.getByRole("tabpanel")).toHaveCount(1);
    await expectMacroLayout(page);
  }
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
    if (viewport.width === 390) {
      const workspace = await page.evaluate(() => {
        const stage = document.querySelector<HTMLElement>(".macro-decision__chart-stage");
        const pair = document.querySelector<HTMLElement>(".macro-decision__chart-pair");
        return {
          pairColumns: pair ? getComputedStyle(pair).gridTemplateColumns : null,
          stageWidth: stage?.clientWidth ?? 0,
        };
      });
      expect(workspace.stageWidth).toBeGreaterThan(300);
      expect(workspace.pairColumns?.split(" ")).toHaveLength(1);
    }
    await expectMacroLayout(page);
  }
  await expectNoUnhandledApiRequests(page);
});

test("contains the wide correlation heatmap in a local mobile scroller", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/macro/cross-asset#correlations");

  await expect(page.getByRole("heading", { name: "跨资产相关性" })).toBeVisible();
  const metrics = await page.evaluate(() => {
    const center = document.querySelector<HTMLElement>(".center-column");
    const scroller = document.querySelector<HTMLElement>(".macro-chart__heatmap-scroll");
    if (!center || !scroller) return null;
    return {
      centerClientWidth: center.clientWidth,
      centerScrollWidth: center.scrollWidth,
      overflowX: getComputedStyle(scroller).overflowX,
      scrollerClientWidth: scroller.clientWidth,
      scrollerScrollWidth: scroller.scrollWidth,
    };
  });

  expect(metrics).not.toBeNull();
  expect(metrics!.centerScrollWidth).toBeLessThanOrEqual(metrics!.centerClientWidth + 1);
  expect(metrics!.overflowX).toBe("auto");
  expect(metrics!.scrollerScrollWidth).toBeGreaterThan(metrics!.scrollerClientWidth);
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
    await expect(page.locator(".macro-decision__header")).toContainText("确定性事实页");
    await expect(page.locator(".macro-decision__diagnostic-strip")).toContainText("当前事实");
    await expect(page.locator(".macro-decision")).not.toContainText("历史窗口");
    await expect(page.locator(".macro-decision")).not.toContainText(
      "展开 Coverage、Current Health、History Depth 与原始事实",
    );
    await expect(page.locator(".macro-decision__semantic-section")).toBeVisible();

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
  ]);
}
