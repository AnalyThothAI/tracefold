import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";

type RouteCase = {
  name: string;
  path: string;
  primary: (page: Page) => Promise<void>;
  specific: (page: Page) => Promise<void>;
  nestedOverflowSelectors?: string[];
  scrollContainerSelector?: string;
  lastMeaningfulSelector: string;
};

const tokenCaseTargetId = tokenCaseFixture().target.target_id;

test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("mobile-"), "mobile-only route layout contract");
});

const routeCases: RouteCase[] = [
  {
    name: "search token result",
    path: "/search?q=HANSA&window=24h",
    primary: async (page) => {
      await expect(page.getByRole("region", { name: "Search Intel" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Search Intel" })).toBeVisible();
    },
    specific: async (page) => {
      const tokenCase = page.getByRole("region", { name: "Token case" });
      await expect(page.getByLabel("global search")).toBeVisible();
      await expect(tokenCase.getByRole("heading", { name: /\$HANSA/ })).toBeVisible();
      await expect(tokenCase.getByRole("heading", { name: "Mention Timeline" })).toBeVisible();
    },
    nestedOverflowSelectors: [".search-intel-page", ".search-dossier", "[aria-label='Token case']"],
    lastMeaningfulSelector: "[aria-labelledby='token-case-timeline'] article:last-of-type",
  },
  {
    name: "stocks",
    path: "/stocks",
    primary: async (page) => {
      await expect(page.getByRole("region", { name: "US stocks radar" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "US Stocks" })).toBeVisible();
    },
    specific: async (page) => {
      const stock = page.getByRole("article", { name: "stock AAPL" });
      await expect(stock).toBeVisible();
      await expect(stock).toContainText("$AAPL");
      await expect(stock).toContainText("AAPL");
      await expect(stock).toContainText("yahoo");
      await expect(page.locator("[aria-label='stocks radar health']")).toContainText("quotes");
    },
    nestedOverflowSelectors: [".stocks-radar-panel", ".stocks-radar-table", ".stock-radar-row"],
    lastMeaningfulSelector: ".stock-radar-row",
  },
  {
    name: "news queue",
    path: "/news",
    primary: async (page) => {
      await expect(page.getByRole("region", { name: "全球新闻" })).toBeVisible();
    },
    specific: async (page) => {
      await expect(page.getByRole("navigation", { name: "新闻视图" })).toBeVisible();
      await expect(page.getByLabel("news search")).toBeVisible();
      await expect(page.getByRole("combobox", { name: "新闻排序" })).toBeVisible();
      await expect(
        page.getByRole("link", { name: /Macro desk flags liquidity rotation/ }),
      ).toBeVisible();
      const rows = page.locator(".news-story-row");
      await expect(rows).toHaveCount(5);
      const fullyVisibleRows = await rows.evaluateAll(
        (elements) =>
          elements.filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.top >= 0 && rect.bottom <= window.innerHeight;
          }).length,
      );
      expect(fullyVisibleRows).toBeGreaterThanOrEqual(2);
    },
    nestedOverflowSelectors: [".news-panel", ".news-story-list", ".news-story-row"],
    lastMeaningfulSelector: ".news-story-row",
  },
  {
    name: "news detail",
    path: "/news/stories/story-global-policy",
    primary: async (page) => {
      await expect(page.getByRole("region", { name: "新闻事件详情" })).toBeVisible();
    },
    specific: async (page) => {
      await expect(page.getByRole("link", { name: "返回全球新闻" })).toBeVisible();
      await expect(page.getByRole("heading", { name: /家独立来源/ })).toBeVisible();
      await expect(
        page.getByRole("heading", {
          exact: true,
          level: 1,
          name: "Macro desk flags liquidity rotation",
        }),
      ).toBeVisible();
      await page.getByText("查看 Tracefold 评分与新闻事件审计").click();
      await expect(page.getByRole("heading", { name: "聚合身份" })).toBeVisible();
      await expect(page.getByRole("heading", { name: /Tracefold 重要度/ })).toBeVisible();
    },
    nestedOverflowSelectors: [".news-panel", ".news-story-detail", ".news-detail-grid"],
    lastMeaningfulSelector: ".news-member-list",
  },
  {
    name: "token case",
    path: `/token/Asset/${encodeURIComponent(tokenCaseTargetId)}?window=1h`,
    primary: async (page) => {
      const tokenCase = page.getByRole("region", { name: "Token case" });
      await expect(tokenCase.getByRole("heading", { name: /\$HANSA/ })).toBeVisible();
    },
    specific: async (page) => {
      const tokenCase = page.getByRole("region", { name: "Token case" });
      await expect(tokenCase.getByRole("heading", { name: "Mention Timeline" })).toBeVisible();
      await expect(tokenCase.getByRole("heading", { name: "Live Market" })).toBeVisible();
      await expect(
        tokenCase.getByRole("article").filter({ hasText: "Expansion leg forming on $HANSA" }),
      ).toBeVisible();
    },
    nestedOverflowSelectors: ["[aria-label='Token case']"],
    lastMeaningfulSelector: "[aria-labelledby='token-case-timeline'] article:last-of-type",
  },
  {
    name: "macro",
    path: "/macro",
    primary: async (page) => {
      await expect(page.getByRole("heading", { level: 1, name: "宏观事实总览" })).toBeVisible();
    },
    specific: async (page) => {
      await expect(page.getByRole("navigation", { name: "宏观页面" })).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "真实利率回落正在缓和风险资产的贴现压力" }),
      ).toBeVisible();
      await expect(page.getByRole("table", { name: "十二资产冻结事实与稀疏展望" })).toBeVisible();
      await expect(page.getByRole("region", { name: "六个宏观模块" })).toBeVisible();
    },
    nestedOverflowSelectors: [
      ".macro-decision",
      ".macro-decision__header",
      ".macro-decision__module-grid",
    ],
    lastMeaningfulSelector: ".macro-decision__module-grid article:last-of-type",
  },
];

for (const routeCase of routeCases) {
  test(`mobile cold-load renders ${routeCase.name} without desktop overflow`, async ({ page }) => {
    await installMockApi(page);
    await page.goto(routeCase.path);

    await routeCase.primary(page);
    await expect(page.getByRole("button", { name: "Toggle Sidebar" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeHidden();
    await expect(page.locator(".live-task-nav")).toHaveCount(0);

    await routeCase.specific(page);
    await expectNoDocumentHorizontalOverflow(page);
    await expectNoNestedHorizontalOverflow(page, [
      ".topbar",
      ...(routeCase.nestedOverflowSelectors ?? []),
    ]);
    await expectScrollableToLastMeaningfulElement(
      page,
      routeCase.scrollContainerSelector ?? ".center-column",
      routeCase.lastMeaningfulSelector,
    );
    await expectNoUnhandledApiRequests(page);
  });
}
