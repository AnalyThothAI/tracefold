import { expect, test, type Page } from "@playwright/test";
import {
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test.setTimeout(60_000);

/*
 * `ready` has to be something that only exists once the data is in, not the static heading: the funnel card,
 * the task-tab counts and the sidebar count all arrive with the status query, and the rows with the feed
 * query. Waiting on the frame alone shot the page mid-fill and made the baselines flaky.
 */
const oiArchetype = {
  // #207: the deterministic OI lane. Its table is the widest thing in the console and scrolls inside
  // itself; the baseline is what keeps that from becoming a page-level horizontal scroll.
  name: "oi",
  path: "/news/oi",
  ready: (page: Page) => page.locator(".news-oi-row").first(),
  settled: (page: Page) => page.locator(".news-oi-policy").first(),
  topbarFigure: "成案 · 放行 · 08-25",
} as const;

const tradingArchetype = {
  name: "trading",
  path: "/trading",
  ready: (page: Page) => page.locator(".trading-exposure-row").first(),
  settled: (page: Page) => page.locator(".trading-ladder-row").first(),
} as const;

const archetypes = [
  {
    name: "news",
    path: "/",
    ready: (page: Page) => page.locator(".news-event-row").first(),
    settled: (page: Page) => page.locator(".news-funnel-card"),
  },
  {
    name: "case",
    path: "/news/events/evt-global-policy",
    ready: (page: Page) => page.locator(".news-detail-hero"),
    settled: (page: Page) => page.locator(".news-timeline-step").first(),
  },
  {
    // #207 PR-W1: the token page composes four endpoints into one column. The baseline is what keeps the
    // identity card, the rank window and the mixed events table from drifting apart at a viewport.
    name: "symbol",
    path: "/news/symbols/WIF",
    ready: (page: Page) => page.locator(".news-symbol-row").first(),
    settled: (page: Page) => page.locator(".news-symbol-contract").first(),
  },
  {
    // #256: a list of cases beside one case in full. The baseline is what keeps the two measures from
    // drifting apart, and what would catch the pane silently disappearing at a viewport.
    name: "leverage",
    path: "/news/leverage",
    ready: (page: Page) => page.getByRole("region", { name: "案例列表" }),
    settled: (page: Page) => page.getByRole("region", { name: /^案例 / }),
  },
  {
    name: "status",
    path: "/news/status",
    ready: (page: Page) => page.locator(".news-health-card").first(),
    settled: (page: Page) => page.getByLabel("过去 24 小时漏斗"),
  },
] as const;

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-07-23T10:00:00Z"));
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await installMockApi(page);
});

test("freezes representative news and case archetypes", async ({ page }) => {
  await freezeArchetypes(page, archetypes);
});

test("freezes the OI monitor at every project viewport", async ({ page }) => {
  await freezeArchetypes(page, [oiArchetype]);
});

test("freezes the Trading workbench at every project viewport", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-08-25T12:00:00Z"));
  await freezeArchetypes(page, [tradingArchetype]);
});

async function freezeArchetypes(
  page: Page,
  routes: readonly (typeof oiArchetype | typeof tradingArchetype | (typeof archetypes)[number])[],
) {
  for (const route of routes) {
    await page.goto(route.path);
    await expect(route.ready(page)).toBeVisible();
    await expect(route.settled(page)).toBeVisible();
    if ("topbarFigure" in route && (page.viewportSize()?.width ?? 0) > 767) {
      await expect(page.locator(".topbar-figures").getByText(route.topbarFigure)).toBeVisible();
    }
    if (route.name === "oi") await expectOiOverflowContract(page);
    if (route.name === "trading") await expectTradingOverflowContract(page);
    if (route.name === "leverage") await expectLeverageScrollContract(page);
    await waitForSettledFeedCount(page);
    await waitForStableWorkbench(page);
    await expect(page).toHaveScreenshot(`archetype-${route.name}.png`, {
      animations: "disabled",
      caret: "hide",
      scale: "css",
      /*
       * The sticky toolbar lands on a sub-pixel boundary that moves by a fraction of a CSS pixel between
       * runs, which at tablet DPR redraws both of its rows identically but one device pixel over — ~0.0003
       * of the frame. This budget absorbs that and nothing else: a real change (a funnel tile that wraps, a
       * chip that gains a venue prefix, a row that loses its badge) is orders of magnitude larger and still
       * fails. Raise it only with a crop showing the difference is genuinely invisible.
       */
      maxDiffPixelRatio: 0.0005,
    });
  }

  await expectNoUnhandledApiRequests(page);
}

/**
 * The case list is a bounded nested scroller (#280), which `docs/FRONTEND.md` allows only with a
 * reachability assertion behind it. Two things have to stay true and neither is visible in a screenshot:
 * the last card must be reachable inside the list, and the funnel below the whole body must be reachable
 * on the page — the second is what `overscroll-behavior: contain` would have quietly broken.
 */
async function expectLeverageScrollContract(page: Page) {
  const list = ".news-leverage-list";
  const bounded = await page.evaluate((selector) => {
    const el = document.querySelector<HTMLElement>(selector);
    if (!el) return null;
    return {
      sticky: getComputedStyle(el).position === "sticky",
      chains: getComputedStyle(el).overscrollBehaviorY !== "contain",
      capped: el.clientHeight <= window.innerHeight,
    };
  }, list);
  expect(bounded).not.toBeNull();
  expect(bounded?.chains, "the list must not trap the wheel above the funnel").toBe(true);
  expect(bounded?.capped, "the list must not grow past the viewport").toBe(true);
  // Desktop only: below 1280 the list is a plain block and the page is the only scroller.
  if ((page.viewportSize()?.width ?? 0) >= 1280) expect(bounded?.sticky).toBe(true);

  await expectScrollableToLastMeaningfulElement(page, list, `${list} > *:last-child`);
  await expectScrollableToLastMeaningfulElement(
    page,
    ".center-column",
    ".news-leverage-funnel .news-source-line",
  );
  // Both scrollers, not just the page: reaching the last card scrolls the list itself, and a baseline
  // taken from there would freeze a half-cropped first card as the intended look.
  await page.evaluate(() => {
    document.querySelector(".center-column")?.scrollTo(0, 0);
    document.querySelector(".news-leverage-list")?.scrollTo(0, 0);
  });
}

async function expectTradingOverflowContract(page: Page) {
  const widths = await page.evaluate(() => {
    const route = document.querySelector<HTMLElement>(".center-column");
    const table = document.querySelector<HTMLElement>(".trading-table");
    return {
      documentClient: document.documentElement.clientWidth,
      documentScroll: document.documentElement.scrollWidth,
      routeClient: route?.clientWidth ?? 0,
      routeScroll: route?.scrollWidth ?? 0,
      tableClient: table?.clientWidth ?? 0,
      tableScroll: table?.scrollWidth ?? 0,
    };
  });
  expect(widths.documentScroll).toBe(widths.documentClient);
  expect(widths.routeScroll).toBe(widths.routeClient);
  if ((page.viewportSize()?.width ?? 0) <= 834) {
    expect(widths.tableScroll).toBeGreaterThan(widths.tableClient);
  }
}

/**
 * The toolbar's `visibleCount / total` grows as the anchored feed fills, and at tablet width the toolbar
 * wraps — so a shot taken while that text is still one digit wide reflows the whole second row on the next
 * render. Waiting for the count to agree with the rows on screen is what makes the tablet baseline stable.
 */
async function waitForSettledFeedCount(page: Page) {
  const total = page.locator(".news-filter-total");
  if (!(await total.count())) return;
  await expect
    .poll(async () => {
      const text = (await total.textContent()) ?? "";
      const shown = Number(text.split("/")[0]?.replace(/[^\d]/g, "") ?? "0");
      return shown === (await page.locator(".news-event-row").count());
    })
    .toBe(true);
}

/** The 1240px frame grid scrolls inside its panel; neither document nor route column may widen. */
async function expectOiOverflowContract(page: Page) {
  const widths = await page.evaluate(() => {
    const route = document.querySelector<HTMLElement>(".center-column");
    const table = document.querySelector<HTMLElement>(".news-oi-table");
    return {
      documentClient: document.documentElement.clientWidth,
      documentScroll: document.documentElement.scrollWidth,
      routeClient: route?.clientWidth ?? 0,
      routeScroll: route?.scrollWidth ?? 0,
      tableClient: table?.clientWidth ?? 0,
      tableScroll: table?.scrollWidth ?? 0,
    };
  });
  expect(widths.documentScroll).toBe(widths.documentClient);
  expect(widths.routeScroll).toBe(widths.routeClient);
  if ((page.viewportSize()?.width ?? 0) <= 1366) {
    expect(widths.tableScroll).toBeGreaterThan(widths.tableClient);
  }
}

async function waitForStableWorkbench(page: Page) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator(".center-column").evaluate((element) => {
    element.scrollTop = 0;
    element.scrollLeft = 0;
  });
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  await expect(page.locator("[data-page-archetype]").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Open ops diagnostics" })).toHaveCount(0);
}
