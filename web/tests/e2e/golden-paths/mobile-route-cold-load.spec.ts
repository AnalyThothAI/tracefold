import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

type RouteCase = {
  name: string;
  path: string;
  primary: (page: Page) => Promise<void>;
  specific: (page: Page) => Promise<void>;
  nestedOverflowSelectors?: string[];
  scrollContainerSelector?: string;
  lastMeaningfulSelector: string;
};

test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("mobile-"), "mobile-only route layout contract");
});

const routeCases: RouteCase[] = [
  {
    name: "news queue",
    path: "/news",
    primary: async (page) => {
      await expect(page.getByRole("region", { name: "新闻事件流" })).toBeVisible();
    },
    specific: async (page) => {
      // Section navigation is the shell's bottom bar on mobile (#87), not on the route.
      await expect(page.getByRole("navigation", { name: "新闻视图" })).toHaveCount(0);
      await expect(page.getByLabel("news search")).toBeVisible();
      await expect(page.getByRole("button", { name: "时间范围，最近 1 天" })).toBeVisible();
      await expect(
        page.getByRole("link", { name: /Macro desk flags liquidity rotation/ }),
      ).toBeVisible();
      const rows = page.locator(".news-event-row");
      await expect(rows).toHaveCount(5);
      /*
       * Only the cards that got somewhere show a badge here; a held card carries its Chinese reason instead
       * (#82). Three quarters of a day's Events are held, and a grey capsule on each of those cards is the
       * screenful of equals this rule exists to prevent.
       */
      const badged = page.locator('.news-event-row:not([data-outcome-group="held"]) .news-outcome');
      await expect(badged.first()).toBeVisible();
      await expect(badged.first()).toContainText("已推送");
      const heldBadge = page.locator('.news-event-row[data-outcome-group="held"] .news-outcome');
      if (await heldBadge.count()) {
        await expect(heldBadge.first()).toBeHidden();
      }
      await expect(page.getByRole("tablist", { name: "按结局筛选" })).toBeVisible();
      const fullyVisibleRows = await rows.evaluateAll(
        (elements) =>
          elements.filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.top >= 0 && rect.bottom <= window.innerHeight;
          }).length,
      );
      expect(fullyVisibleRows).toBeGreaterThanOrEqual(2);
    },
    nestedOverflowSelectors: [".news-panel", ".news-event-list", ".news-event-row"],
    lastMeaningfulSelector: ".news-event-row",
  },
  {
    name: "news detail",
    path: "/news/events/evt-global-policy",
    primary: async (page) => {
      await expect(page.getByRole("region", { name: "新闻事件详情" })).toBeVisible();
    },
    specific: async (page) => {
      await expect(page.getByRole("link", { name: "返回新闻事件流" })).toBeVisible();
      await expect(
        page.getByRole("heading", { exact: true, level: 1, name: "央行政策转向，风险资产承压" }),
      ).toBeVisible();
      await expect(page.locator(".news-detail-original")).toContainText(
        "Macro desk flags liquidity rotation",
      );
      await expect(page.locator(".news-detail-hero .news-outcome")).toContainText("已推送");
      await expect(page.getByRole("heading", { name: "这条新闻经历了什么" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "同类报道" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "学习复盘" })).toBeVisible();
      await expect(page.getByRole("link", { name: "在学习复盘中打开" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "市场标记" })).toHaveCount(0);
    },
    nestedOverflowSelectors: [
      ".news-panel",
      ".news-detail-hero",
      ".news-timeline",
      ".news-detail-grid",
    ],
    lastMeaningfulSelector: ".news-technical",
  },
];

for (const routeCase of routeCases) {
  test(`mobile cold-load renders ${routeCase.name} without desktop overflow`, async ({ page }) => {
    await installMockApi(page);
    await page.goto(routeCase.path);

    await routeCase.primary(page);
    // #87: the drawer is gone on a phone; the bottom bar carries navigation and is always visible.
    await expect(page.getByRole("button", { name: "Toggle Sidebar" })).toHaveCount(0);
    await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
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
