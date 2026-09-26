import { allowBrowserFailure, expect, test, type Page } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import {
  newsMarketFixture,
  newsMarketGroupFixture,
  newsMarketItemFixture,
  newsMarketObservationFixture,
} from "@tests/fixtures/newsFixture";
import {
  tradingCaseFixture,
  tradingCasesFixture,
  tradingExecutionRowFixture,
  tradingExecutionsFixture,
  tradingLiveExecutionFixture,
} from "@tests/fixtures/tradingFixture";

const origin = "/news/market?kind=oi&asset=WIF&from_ms=1778990000000&to_ms=1779000000000";
const observation = newsMarketObservationFixture({
  raw_instrument: "WIFUSDT",
  measurement_contract_status: "proven",
  measurement_definition: "oi_notional_change",
  measurement_window_ms: 900000,
});
const decision = tradingCaseFixture({
  case_id: "case-wif-research",
  base_symbol: "WIF",
  market_key: "crypto:perp:WIF:USDT",
  trigger_id: "trigger-wif",
  trigger_kind: "oi",
  source_item_id: observation.item_id,
  state: "DONE",
  analysis_action: "TRADE",
  analysis_publish_status: "published",
  analysis_side: "long",
  analysis_decision: {
    action: "TRADE",
    decided_at_ms: observation.event_at_ms + 1000,
    decision: { side: "long", reason: "OI 变化与冻结的候选计划相符；执行结果以场所记录为准。" },
    decision_id: "decision-wif",
    policy_id: "trade_assessment_v1",
    policy_version: "v1",
    publish_status: "published",
    valid_until_ms: observation.event_at_ms + 60000,
  },
});

async function installResearchScenario(page: Page) {
  await installMockApi(page, {
    tradingExecution: tradingLiveExecutionFixture({ facts_expire_at_ms: Date.now() + 300000 }),
  });
  const calls: { source: string[]; execution: string[]; mutations: string[] } = {
    source: [],
    execution: [],
    mutations: [],
  };
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET")
      calls.mutations.push(request.method());
  });
  await page.route(/\/api\/news\/market(?:\/[^/?]+)?(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/news/market") {
      return route.fulfill({
        json: {
          ok: true,
          data: newsMarketFixture({ groups: [newsMarketGroupFixture({ latest: observation })] }),
        },
      });
    }
    const selected = {
      ...observation,
      item_id: decodeURIComponent(url.pathname.split("/").pop()!),
    };
    return route.fulfill({
      json: {
        ok: true,
        data: newsMarketItemFixture({
          observation: selected,
          timeline: [
            selected,
            {
              ...selected,
              item_id: "earlier-wif",
              event_at_ms: selected.event_at_ms - 600000,
              oi_change_bps: 512,
            },
          ],
        }),
      },
    });
  });
  await page.route("**/api/trading/cases*", async (route) => {
    const url = new URL(route.request().url());
    const source = url.searchParams.get("source_item_id");
    if (source) calls.source.push(source);
    const cases =
      url.searchParams.get("case_id") === decision.case_id ||
      url.searchParams.get("view") === "list"
        ? [decision]
        : [];
    return route.fulfill({
      json: { ok: true, data: tradingCasesFixture({ cases, total: cases.length }) },
    });
  });
  await page.route("**/api/trading/executions*", async (route) => {
    const caseId = new URL(route.request().url()).searchParams.get("case_id");
    if (caseId) calls.execution.push(caseId);
    return route.fulfill({
      json: {
        ok: true,
        data: tradingExecutionsFixture({
          executions: caseId
            ? [
                tradingExecutionRowFixture({
                  case_id: decision.case_id,
                  entry_id: "entry-wif",
                  market_key: "crypto:perp:WIF:USDT",
                }),
              ]
            : [],
        }),
      },
    });
  });
  return calls;
}

test("an observation retains its research context through Case, execution and hard reload", async ({
  page,
  baseURL,
}, testInfo) => {
  const calls = await installResearchScenario(page);
  allowBrowserFailure(page, {
    kind: "requestfailed",
    match: "GET /api/trading/status (net::ERR_ABORTED)",
    reason: "This case deliberately reloads the page twice; navigation can cancel an in-flight status poll.",
  });
  await page.goto(origin);
  const row = page.locator(".news-market-row-main").first();
  await row.click();
  const evidence = page.getByRole("dialog", { name: "市场观察依据" });
  await expect(evidence.getByRole("region", { name: "同口径离散 OI 观察" })).toBeVisible();
  const returnPath = new URL(page.url()).pathname + new URL(page.url()).search;
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("market-research.png"), fullPage: true });
  await page.reload();
  await expect(evidence).toBeVisible();
  await evidence.getByRole("link", { name: "查看这条观察的策略判定" }).click();
  await expect(page.getByRole("heading", { name: "这条观察关联的策略判定" })).toBeVisible();
  expect(calls.source).toContain(observation.item_id);
  await page.locator(".trading-case-list-row").first().click();
  const detail = page.getByRole("dialog", { name: "策略判定依据" });
  await expect(detail.getByText("Signal 已发布 · 做多")).toBeVisible();
  await expect(detail.getByText("冻结判定字段与来源身份").locator("..")).not.toHaveAttribute(
    "open",
  );
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("strategy-evidence.png"), fullPage: true });
  await detail.getByRole("link", { name: "查看关联执行" }).click();
  await expect(page.getByRole("heading", { name: "关联执行记录" })).toBeVisible();
  expect(calls.execution).toContain(decision.case_id);
  await page.getByRole("button", { name: "执行明细", exact: true }).click();
  await expect(page.getByRole("region", { name: "执行明细 crypto:perp:WIF:USDT" })).toBeVisible();
  await page.reload();
  expect(new URL(page.url()).searchParams.get("entry")).toBe("entry-wif");
  await expect(page.getByText("冻结止损 200 bps")).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await page.getByRole("link", { name: "返回原始观察与筛选" }).click();
  await expect(page).toHaveURL(new URL(returnPath, baseURL).href);
  await expect(evidence).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(evidence).toHaveCount(0);
  await row.click();
  await page.keyboard.press("Escape");
  await expect(row).toBeFocused();
  expect(calls.mutations).toEqual([]);
  await expectNoUnhandledApiRequests(page);
});

test("positions and recent decisions stay separate, and source identity survives detail reload", async ({
  page,
  baseURL,
}, testInfo) => {
  await installResearchScenario(page);
  await page.goto("/trading");
  await expect(page.getByLabel("已记录的退出价格区间")).toBeVisible();
  await expect(page.getByText("BTCUSDT-PERP.BINANCE").first()).toBeVisible();
  await expect(page.locator(".trading-recent")).toContainText("WIF");
  await expectNoDocumentHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("execution-positions.png"), fullPage: true });
  const recent = page.locator(".trading-recent-row").first();
  await recent.click();
  await expect(page.getByRole("dialog", { name: "策略判定依据" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "策略判定依据" })).toHaveCount(0);
  await expect(recent).toBeFocused();
  const from = origin + "&item=different-observation";
  await page.goto(
    `/trading?tab=decisions&case=${decision.case_id}&research_from=${encodeURIComponent(from)}`,
  );
  await page.getByRole("link", { name: "查看原始观察依据" }).click();
  await expect(page).toHaveURL(new RegExp(`/news/market/${observation.item_id}\\?`));
  await page.reload();
  await page.getByRole("link", { name: "返回研究列表" }).click();
  await expect(page).toHaveURL(new URL(from, baseURL).href);
  await expect(page.getByRole("dialog", { name: "市场观察依据" })).toBeVisible();
  await expectNoUnhandledApiRequests(page);
});
