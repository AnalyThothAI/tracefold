import { expect, test, type Page } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi, type MockApiOptions } from "@tests/e2e/support/mockApi";
import {
  tradingCommandRowFixture,
  tradingCurrentAccountFixture,
  tradingExecutionRowFixture,
  tradingExecutionsFixture,
  tradingLiveExecutionFixture,
} from "@tests/fixtures/tradingFixture";

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
  topbarFigure: "7 · 1",
} as const;

const tradingArchetype = {
  name: "trading",
  path: "/trading",
  ready: (page: Page) => page.locator(".trading-safety-grid"),
  settled: (page: Page) => page.getByRole("heading", { name: "今日执行" }),
} as const;

/*
 * The state `/news/alpha` used to own (#460): one Case open, with the frozen check table and the frozen
 * config beneath it. Kept as its own archetype rather than folded into `trading` because the two are
 * different pictures — a status page of six ledgers, and that page with a document expanded inside one
 * of them — and a single baseline could only hold one of them.
 */
const tradingCaseArchetype = {
  name: "trading-case",
  path: "/trading?case=case-hype",
  ready: (page: Page) => page.getByRole("region", { name: /^案例 / }),
  settled: (page: Page) => page.getByRole("heading", { name: /^冻结判定证据 / }),
} as const;

const symbolArchetype = {
  // The token page composes several endpoints into one column, including the Signal lane's Case/Signal
  // projection. The baseline keeps the identity band, rank window, Alpha 复盘 and
  // the mixed events table from drifting apart at a viewport.
  name: "symbol",
  path: "/news/symbols/WIF",
  ready: (page: Page) => page.locator(".news-symbol-row").first(),
  settled: (page: Page) => page.locator(".news-symbol-contract").first(),
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

/*
 * The pages that read the Alpha/Signal ledger are frozen on the ledger's own clock, because the trading
 * fixtures are on it and the news fixtures are a hundred days earlier. Under the news clock the token
 * page's newest case — and the frame it was opened from — sat a month in the page's own *future*, which
 * `relativeTime` clamps to 「刚刚」: a baseline of a state the pipeline cannot produce.
 */
test("freezes the Alpha and execution pages at every project viewport", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-08-25T12:00:00Z"));
  await freezeArchetypes(page, [symbolArchetype, tradingArchetype, tradingCaseArchetype]);
});

test("writes an operator command in one click and claims only persistence", async ({ page }) => {
  await page.unroute("**/api/**");
  await installMockApi(page, { tradingExecution: tradingLiveExecutionFixture() });
  await page.goto("/trading");
  await expect(page.locator(".trading-safety-grid")).toBeVisible();

  const posted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/trading/execution/commands",
  );
  await page.getByRole("button", { name: "Resume / Arm" }).click();
  const request = await posted;

  expect(page.getByRole("alertdialog")).toHaveCount(0);
  expect(request.headers().authorization).toBe("Bearer secret");
  expect(request.postDataJSON()).toMatchObject({ text: "/resume operator console" });
  await expect(page.getByText(/Command 已持久化/)).toContainText(
    "这不代表 Runtime 受理、订单或成交",
  );
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".trading-safety-grid",
    ".trading-account-overview",
    ".trading-control-panel",
    ".trading-position-row",
    ".trading-command-row",
  ]);
  await expectNoUnhandledApiRequests(page);
});

test("keeps execution truth distinct across unsafe, protected, flat, and expired states", async ({
  page,
}) => {
  await page.unroute("**/api/**");
  const options: MockApiOptions = {};
  await installMockApi(page, options);

  options.tradingExecution = tradingLiveExecutionFixture({
    current_account: null,
    entry_block_reason: "reconciliation_stale",
    execution_safe: false,
    open_orders_count: 0,
    positions_count: 0,
    protection_status: "unknown",
  });
  await page.goto("/trading?browser-state=alive-unsafe");
  let safety = page.getByLabel("执行安全状态");
  await expect(safety.locator(".ui-metric").nth(0)).toContainText("YES");
  await expect(safety.locator(".ui-metric").nth(1)).toContainText("NO");
  await expect(page.getByText("私有对账已过期")).toBeVisible();

  options.tradingExecution = tradingLiveExecutionFixture({
    current_account: tradingCurrentAccountFixture({ unknown_orders_count: 1 }),
  });
  await page.goto("/trading?browser-state=protected");
  await expect(page.getByText("FULL COVERAGE")).toBeVisible();
  await expect(page.getByText("Unknown").locator("..")).toContainText("1");

  const unprotectedAccount = tradingCurrentAccountFixture();
  options.tradingExecution = tradingLiveExecutionFixture({
    current_account: tradingCurrentAccountFixture({
      complete: false,
      positions: [
        {
          ...unprotectedAccount.positions![0]!,
          protection_full_coverage: false,
          protection_quantity: null,
          protection_status: "unprotected",
          protection_trigger_price: null,
        },
      ],
    }),
    entry_block_reason: "unexpected_exposure",
    execution_safe: false,
    protection_status: "unprotected",
  });
  await page.goto("/trading?browser-state=unprotected");
  await expect(page.getByText("UNPROTECTED").first()).toBeVisible();
  await expect(page.getByText("NOT FULLY COVERED")).toBeVisible();
  await expect(page.getByText("PARTIAL")).toBeVisible();

  // Every stage the read model can report for one Command, straight from `control_disposition`.
  options.tradingExecutions = tradingExecutionsFixture({
    commands: [tradingCommandRowFixture({ action: "flatten", stage: "recorded" })],
    executions: [],
  });
  await page.goto("/trading?browser-state=flatten-recorded");
  await expect(page.getByText("已持久化").first()).toBeVisible();

  options.tradingExecutions = tradingExecutionsFixture({
    commands: [
      tradingCommandRowFixture({ action: "flatten", stage: "completed" }),
      tradingCommandRowFixture({ command_id: "d".repeat(64), stage: "rejected" }),
    ],
    executions: [],
  });
  await page.goto("/trading?browser-state=flatten-completed");
  await expect(page.getByText("已完成 · 私有对账证明").first()).toBeVisible();
  await expect(page.getByText("Runtime 拒绝").first()).toBeVisible();

  // And the whole Signal ledger the desk exists for: one row from ordered to closed with a number on it.
  options.tradingExecutions = tradingExecutionsFixture({
    commands: [],
    executions: [tradingExecutionRowFixture()],
  });
  options.tradingExecution = tradingLiveExecutionFixture({
    account_flat: true,
    account_flat_proven: true,
    current_account: tradingCurrentAccountFixture({
      aggregate_risk_usd: "0",
      inflight_orders_count: 0,
      open_orders_count: 0,
      orders: [],
      positions: [],
      unknown_orders_count: 0,
    }),
    open_orders_count: 0,
    positions_count: 0,
    protection_status: "not_applicable",
  });
  await page.goto("/trading?browser-state=flat-proven");
  safety = page.getByLabel("执行安全状态");
  await expect(safety.locator(".ui-metric").nth(3)).toContainText("PROVEN");
  await expect(page.getByText("已平仓").first()).toBeVisible();
  await expect(page.getByText("flatten 退出").first()).toBeVisible();
  await expect(page.getByText(/新鲜 Binance 私有对账已证明账户为空/)).toBeVisible();

  /*
   * Past the instant `/status` published as the end of its own budget, all four safety words read 过期.
   * The page runs no timer to get there: the expiry is absolute, and the comparison is against the clock
   * this test fixed above.
   */
  options.tradingExecution = tradingLiveExecutionFixture({
    account_flat: true,
    account_flat_proven: true,
    entries_armed: true,
    entries_paused: false,
    entry_block_reason: null,
    facts_expire_at_ms: Date.parse("2026-08-25T11:59:00Z"),
  });
  await page.goto("/trading?browser-state=facts-expired");
  await expect(page.getByLabel("执行安全状态").getByText("过期")).toHaveCount(4);
  await expect(page.getByText(/本次读取的事实已过期/)).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

async function freezeArchetypes(
  page: Page,
  routes: readonly (
    | typeof oiArchetype
    | typeof tradingArchetype
    | typeof tradingCaseArchetype
    | typeof symbolArchetype
    | (typeof archetypes)[number]
  )[],
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
    if (route.name === "trading-case") await expectTradingCaseOverflowContract(page);
    await waitForSettledFeedCount(page);
    await waitForStableWorkbench(page);
    if (route.name === "trading-case") {
      await route.settled(page).scrollIntoViewIfNeeded();
    }
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
 * The Case detail is a responsive ledger inside the Case card. Neither it nor the frozen configuration
 * may push the document sideways, and the final row must remain reachable through the page scroller.
 */
async function expectTradingCaseOverflowContract(page: Page) {
  const widths = await page.evaluate(() => {
    const route = document.querySelector<HTMLElement>(".center-column");
    return {
      documentClient: document.documentElement.clientWidth,
      documentScroll: document.documentElement.scrollWidth,
      routeClient: route?.clientWidth ?? 0,
      routeScroll: route?.scrollWidth ?? 0,
    };
  });
  expect(widths.documentScroll).toBe(widths.documentClient);
  expect(widths.routeScroll).toBe(widths.routeClient);

  await expectScrollableToLastMeaningfulElement(
    page,
    ".center-column",
    ".trading-case-detail > *:last-child",
  );
  await page.evaluate(() => {
    document.querySelector(".center-column")?.scrollTo(0, 0);
  });
}

async function expectTradingOverflowContract(page: Page) {
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".center-column",
    ".trading-safety-grid",
    ".trading-account-overview",
    ".trading-control-panel",
    ".trading-funnel-grid",
    ".trading-case-list",
    ".trading-case-card",
    ".trading-command-row",
  ]);
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

/** The 1244px frame grid scrolls inside its panel; neither document nor route column may widen. */
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
