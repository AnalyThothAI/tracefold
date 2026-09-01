import { expect, test, type Page } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi, type MockApiOptions } from "@tests/e2e/support/mockApi";
import {
  tradingCommandFixture,
  tradingCommandsFixture,
  tradingCurrentAccountFixture,
  tradingExecutionFixture,
  tradingObservationFixture,
  tradingObservationsFixture,
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
  settled: (page: Page) => page.getByRole("heading", { name: "Command 进度" }),
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

test("confirms an operator command without claiming Runtime or venue success", async ({ page }) => {
  await page.unroute("**/api/**");
  await installMockApi(page, {
    tradingExecution: tradingExecutionFixture({
      account_flat: false,
      account_flat_proven: false,
      alive: true,
      current_account: tradingCurrentAccountFixture(),
      entries_armed: false,
      entries_paused: true,
      entry_block_reason: "entries_paused",
      execution_safe: true,
      mode: "paper",
      open_orders_count: 1,
      positions_count: 1,
      protection_status: "protected",
      reconciliation_age_ms: 1_000,
      startup_reconciled: true,
    }),
  });
  await page.goto("/trading");
  await expect(page.locator(".trading-safety-grid")).toBeVisible();

  const posted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/trading/execution/commands",
  );
  await page.getByLabel("Operator write token").fill("operator-write-token");
  await page.getByRole("button", { name: "Resume / Arm" }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("button", { name: "确认写入 Command" }).click();
  const request = await posted;

  expect(request.headers().authorization).toBe("Bearer operator-write-token");
  expect(request.postDataJSON()).toMatchObject({ text: "/resume operator console CONFIRM" });
  await expect(page.getByText(/Command 已持久化/)).toContainText(
    "这不代表 Runtime 受理、订单或成交",
  );
  await page.getByText("Advanced Audit").click();
  await expect(page.getByText(/profile/)).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".trading-safety-grid",
    ".trading-account-overview",
    ".trading-control-panel",
    ".trading-latest-progress-card",
    ".trading-position-row",
    ".trading-ledger-row",
  ]);
  await expectNoUnhandledApiRequests(page);
});

test("keeps execution truth distinct across unsafe, recovery, flat, and stale states", async ({
  page,
}) => {
  await page.unroute("**/api/**");
  const options: MockApiOptions = {};
  await installMockApi(page, options);

  options.tradingExecution = tradingExecutionFixture({
    alive: true,
    entries_armed: false,
    entries_paused: true,
    entry_block_reason: "private_reconciliation_unavailable",
    execution_safe: false,
    mode: "paper",
  });
  await page.goto("/trading?browser-state=alive-unsafe");
  let safety = page.getByLabel("执行安全状态");
  await expect(safety.locator(".ui-metric").nth(0)).toContainText("YES");
  await expect(safety.locator(".ui-metric").nth(1)).toContainText("NO");

  options.tradingExecution = tradingExecutionFixture({
    account_flat: false,
    account_flat_proven: false,
    alive: true,
    current_account: tradingCurrentAccountFixture({ unknown_orders_count: 1 }),
    entries_armed: false,
    entries_paused: true,
    entry_block_reason: "entries_paused",
    execution_safe: true,
    mode: "paper",
    open_orders_count: 1,
    positions_count: 1,
    protection_status: "protected",
    reconciliation_age_ms: 1_000,
    startup_reconciled: true,
  });
  await page.goto("/trading?browser-state=protected");
  await expect(page.getByText("FULL COVERAGE")).toBeVisible();
  await expect(page.getByText("Unknown").locator("..")).toContainText("1");

  const unprotectedAccount = tradingCurrentAccountFixture();
  options.tradingExecution = tradingExecutionFixture({
    account_flat: false,
    account_flat_proven: false,
    alive: true,
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
    entries_armed: false,
    entries_paused: true,
    entry_block_reason: "protection_unavailable",
    execution_safe: false,
    mode: "paper",
    open_orders_count: 0,
    positions_count: 1,
    protection_status: "unprotected",
    reconciliation_age_ms: 1_000,
    startup_reconciled: true,
  });
  await page.goto("/trading?browser-state=unprotected");
  await expect(page.getByText("UNPROTECTED").first()).toBeVisible();
  await expect(page.getByText("NOT FULLY COVERED")).toBeVisible();

  const flatten = tradingCommandFixture({ action: "flatten", confirmed: true });
  options.tradingCommands = tradingCommandsFixture({ commands: [flatten] });
  options.tradingObservations = tradingObservationsFixture({
    observations: [
      tradingObservationFixture({
        command_id: flatten.command_id,
        normalized_kind: "readiness",
        summary: { control_stage: "runtime_accepted" },
      }),
    ],
  });
  await page.goto("/trading?browser-state=flatten-pending");
  await expect(page.getByText("RUNTIME ACCEPTED").first()).toBeVisible();
  await expect(page.getByText("等待 venue").first()).toBeVisible();

  const acceptedOrder = tradingObservationFixture({
    command_id: flatten.command_id,
    event_id: "1".repeat(64),
    normalized_kind: "order",
    summary: { status: "accepted" },
  });
  options.tradingObservations = tradingObservationsFixture({
    observations: [
      tradingObservationFixture({
        command_id: flatten.command_id,
        normalized_kind: "readiness",
        summary: { control_stage: "runtime_accepted" },
      }),
      acceptedOrder,
    ],
  });
  await page.goto("/trading?browser-state=order-accepted");
  await expect(page.getByText("ORDER ACCEPTED").first()).toBeVisible();

  options.tradingObservations = tradingObservationsFixture({
    observations: [
      acceptedOrder,
      tradingObservationFixture({
        command_id: flatten.command_id,
        event_id: "2".repeat(64),
        normalized_kind: "fill",
      }),
    ],
  });
  await page.goto("/trading?browser-state=fill-observed");
  await expect(page.getByText("FILL OBSERVED").first()).toBeVisible();

  options.tradingCommands = tradingCommandsFixture({
    commands: [tradingCommandFixture({ disposition: "rejected" })],
  });
  options.tradingObservations = tradingObservationsFixture();
  await page.goto("/trading?browser-state=runtime-rejected");
  await expect(page.getByText("RUNTIME REJECTED").first()).toBeVisible();

  options.tradingCommands = tradingCommandsFixture({
    commands: [tradingCommandFixture({ expired: true })],
  });
  await page.goto("/trading?browser-state=expired");
  await expect(page.getByText("EXPIRED").first()).toBeVisible();

  options.tradingCommands = tradingCommandsFixture({
    commands: [
      tradingCommandFixture({
        action: "flatten",
        confirmed: true,
        disposition: "completed",
        disposition_reason: "binance_account_flat",
      }),
    ],
  });
  options.tradingExecution = tradingExecutionFixture({
    account_flat: true,
    account_flat_proven: true,
    alive: true,
    current_account: tradingCurrentAccountFixture({
      aggregate_risk_usd: "0",
      inflight_orders_count: 0,
      open_orders_count: 0,
      orders: [],
      positions: [],
      unknown_orders_count: 0,
    }),
    entries_armed: false,
    entries_paused: true,
    entry_block_reason: "entries_paused",
    execution_safe: true,
    mode: "paper",
    open_orders_count: 0,
    positions_count: 0,
    protection_status: "not_applicable",
    reconciliation_age_ms: 1_000,
    startup_reconciled: true,
  });
  await page.goto("/trading?browser-state=flat-proven");
  safety = page.getByLabel("执行安全状态");
  await expect(safety.locator(".ui-metric").nth(3)).toContainText("PROVEN");
  await expect(page.getByText("ACCOUNT FLAT · PROVEN").first()).toBeVisible();
  await expect(page.getByText(/新鲜 Binance 私有对账已证明账户为空/)).toBeVisible();

  options.tradingCommands = tradingCommandsFixture();
  options.tradingObservations = tradingObservationsFixture();
  options.tradingExecution = tradingExecutionFixture({
    account_flat: true,
    account_flat_proven: false,
    alive: true,
    current_account: tradingCurrentAccountFixture({ complete: false }),
    entries_armed: false,
    entries_paused: true,
    entry_block_reason: "reconciliation_stale",
    execution_safe: false,
    mode: "paper",
    reconciliation_age_ms: 15_000,
    startup_reconciled: true,
  });
  await page.goto("/trading?browser-state=stale-partial");
  await expect(page.getByText("PARTIAL")).toBeVisible();
  await expect(page.getByText("15,000 ms")).toBeVisible();
  await expect(page.getByText("NOT PROVEN")).toBeVisible();
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
    ".trading-latest-progress-card",
    ".trading-case-list",
    ".trading-case-card",
    ".trading-ledger-row",
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
