import type { NewsOiTradeFloors } from "@features/news/api/newsQueries";
import {
  LEVERAGE_TABS,
  evidenceRows,
  leverageCases,
  leverageFramelessCount,
  leverageTabCount,
  leverageTimeline,
  parseLeverageTab,
} from "@features/news/model/leverageCases";
import { tradingOiLedgerByEventId } from "@features/trading";
import { newsOiFrameFixture, newsStatusFixture } from "@tests/fixtures/newsFixture";
import {
  tradingCaseFixture,
  tradingOrderFixture,
  tradingOrdersFixture,
} from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

const FLOORS: NewsOiTradeFloors = newsStatusFixture().oi!.trade_floors!;
const NOW_MS = 1_756_000_000_000;

function build(events = [newsOiFrameFixture()], orders = tradingOrdersFixture()) {
  return leverageCases(events, tradingOiLedgerByEventId(orders), FLOORS, NOW_MS);
}

describe("leverageCases", () => {
  it("takes the ledger as the authority for which cases exist, not one page of frames", () => {
    /*
     * Two directions, and only one is correct. A frame with no ledger entry is not a case and is not
     * listed — the OI audit owns that whole population. But a *case* whose frame is older than the loaded
     * page is still a case, and walking frames would have dropped it while the page's figures claimed to
     * describe the lane's whole 24 h load.
     */
    const frames = [
      newsOiFrameFixture({ event_id: "evt-oi-wif" }),
      newsOiFrameFixture({ event_id: "evt-never-judged" }),
    ];
    const ledger = tradingOrdersFixture({
      cases_without_orders: [tradingCaseFixture({ event_id: "evt-off-page" })],
      orders: [tradingOrderFixture({ event_id: "evt-oi-wif" })],
    });

    const ids = build(frames, ledger).map((item) => item.id);
    expect(ids).toContain("evt-oi-wif");
    expect(ids).toContain("evt-off-page");
    expect(ids).not.toContain("evt-never-judged");
  });

  it("treats a blocked case as terminal, not as one still forming", () => {
    /*
     * `_place` refusing — daily order cap, one position per underlying, a blacklist re-read, a sizing
     * rejection — settles the case `BLOCKED` while keeping the strategy's direction in `policy_decision`.
     * Reading that direction as "still forming" put a dead case under 正在发生 with a red LONG chip and a
     * heading promising a live setup.
     */
    const [item] = build(
      [newsOiFrameFixture({ event_id: "evt-blocked" })],
      tradingOrdersFixture({
        cases_without_orders: [
          tradingCaseFixture({
            event_id: "evt-blocked",
            policy_decision: "long",
            policy_reason: "order_blocked",
            state: "BLOCKED",
          }),
        ],
        orders: [],
      }),
    );

    expect(item.phase).toBe("no_trade");
    expect(LEVERAGE_TABS.live.predicate(item)).toBe(false);
  });

  it("reads each phase from the ledger's own state, never from a clock", () => {
    const frames = [
      newsOiFrameFixture({ event_id: "evt-open" }),
      newsOiFrameFixture({ event_id: "evt-closed" }),
      newsOiFrameFixture({ event_id: "evt-rejected" }),
    ];
    const ledger = tradingOrdersFixture({
      cases_without_orders: [
        tradingCaseFixture({ event_id: "evt-rejected", state: "POLICY_REJECTED" }),
      ],
      orders: [
        tradingOrderFixture({ event_id: "evt-open", state: "OPEN" }),
        tradingOrderFixture({ event_id: "evt-closed", order_id: "o2", state: "CLOSED" }),
      ],
    });

    const byId = Object.fromEntries(build(frames, ledger).map((item) => [item.id, item.phase]));
    expect(byId).toEqual({
      "evt-closed": "resolved",
      "evt-open": "active",
      "evt-rejected": "no_trade",
    });
  });

  it("puts capital at risk first and never invents a composite score", () => {
    const frames = ["evt-a", "evt-b", "evt-c"].map((event_id) => newsOiFrameFixture({ event_id }));
    const ledger = tradingOrdersFixture({
      cases_without_orders: [],
      orders: [
        // Newest, but merely open.
        tradingOrderFixture({
          case_observed_at_ms: NOW_MS - 1_000,
          event_id: "evt-a",
          state: "OPEN",
        }),
        // Older, and unreconciled: this is the row an operator has to see first.
        tradingOrderFixture({
          case_observed_at_ms: NOW_MS - 900_000,
          event_id: "evt-b",
          order_id: "o2",
          state: "AMBIGUOUS",
        }),
        tradingOrderFixture({
          case_observed_at_ms: NOW_MS - 500_000,
          event_id: "evt-c",
          order_id: "o3",
          state: "CLOSED",
        }),
      ],
    });

    expect(build(frames, ledger).map((item) => item.id)).toEqual(["evt-b", "evt-a", "evt-c"]);
  });

  it("names the rule rather than paraphrasing a thesis nobody wrote", () => {
    // `oi_momentum_v1` is a pure rule and produces no narrative at all; inventing a sentence for it would
    // be the console asserting a reason the ledger never recorded.
    const [item] = build(
      [newsOiFrameFixture({ event_id: "evt-oi-hype" })],
      tradingOrdersFixture({ orders: [] }),
    );

    expect(item.decision).toBe("no_trade");
    expect(item.why).toBe("不交易：鲸盈利未达地板。");
    expect(item.rule).toBe("whale_profit_below_floor");
  });
});

describe("leverageFramelessCount", () => {
  it("counts the listed cases whose frame is off the loaded page", () => {
    /*
     * Frames arrive one bounded page at a time and the ledger as its own batch. Every case is listed
     * either way — the frame is decoration on a case, not its identity — but rows without one carry no
     * wire line and no OI measurements, and a page that showed them silently would look like a lane that
     * stopped measuring.
     */
    const cases = build(
      [newsOiFrameFixture({ event_id: "evt-oi-wif" })],
      tradingOrdersFixture({
        cases_without_orders: [tradingCaseFixture({ event_id: "evt-off-page" })],
        orders: [tradingOrderFixture({ event_id: "evt-oi-wif" })],
      }),
    );

    expect(cases).toHaveLength(2);
    expect(leverageFramelessCount(cases)).toBe(1);
    // A page boundary and a broken provider line are different facts and must read differently.
    const frameless = cases.find((item) => item.event === undefined)!;
    expect(frameless.numbers).toBe("原帧不在本页帧里");
    expect(frameless.evidence.find((row) => row.key === "oi")?.note).toBe(
      "原帧不在本页帧里（帧按页取）",
    );
  });
});

describe("evidenceRows", () => {
  it("keeps 缺失 a first-class answer instead of omitting the row", () => {
    /*
     * The pre-frame move, funding and liquidity are inputs the capital lane consumes and does not publish.
     * A matrix that dropped the rows it cannot fill would read as "everything checked out".
     */
    const rows = evidenceRows(newsOiFrameFixture().oi, "oi_momentum_v1", FLOORS);

    expect(rows.map((row) => row.key)).toEqual([
      "oi",
      "value",
      "whale",
      "price",
      "news",
      "funding",
      "liquidity",
    ]);
    expect(rows.filter((row) => row.status === "missing").map((row) => row.key)).toEqual([
      "price",
      "funding",
      "liquidity",
    ]);
    // The WIF fixture is below both capital floors and above zero OI change — three real answers.
    expect(rows.find((row) => row.key === "oi")?.status).toBe("support");
    expect(rows.find((row) => row.key === "value")?.status).toBe("conflict");
    expect(rows.find((row) => row.key === "whale")?.status).toBe("conflict");
    expect(rows.find((row) => row.key === "news")?.status).toBe("na");
  });

  it("never reports News as supporting a case whose alignment it cannot see", () => {
    /*
     * The strategy *requiring* alignment is not the strategy having *found* it: `news_oi_alignment_v1`
     * rejects with `news_context_missing`, `model_contradicts_regime` and four other alignment failures,
     * and none of them reaches this browser. Stamping 支持 on the strategy id put "the news supports this"
     * on a case rejected because the news contradicted the regime.
     */
    const rows = evidenceRows(newsOiFrameFixture().oi, "news_oi_alignment_v1", FLOORS);

    expect(rows.find((row) => row.key === "news")?.status).toBe("missing");
  });

  it("refuses to call an unconfigured floor a pass", () => {
    // A zero floor arrives when the console is newer than the API. `measured >= 0` would stamp 支持 on
    // every frame against a threshold nobody set.
    const rows = evidenceRows(newsOiFrameFixture().oi, "oi_momentum_v1", {
      ...FLOORS,
      min_oi_value_usd: 0,
      min_whale_long_profit_bps: 0,
    });

    expect(rows.find((row) => row.key === "value")?.status).toBe("na");
    expect(rows.find((row) => row.key === "whale")?.status).toBe("na");
  });
});

describe("leverageTimeline", () => {
  it("orders the steps by the clock the ledger wrote, not by the stage names", () => {
    /*
     * `settle_case` stamps `decided_at_ms` after the case row exists, so a timeline printed in stage order
     * ran its visible clock backwards between two adjacent rows — under a heading promising the ledger's
     * own timestamps.
     */
    const [withOrder] = build();
    expect(leverageTimeline(withOrder).map((step) => step.key)).toEqual([
      "trigger",
      "case",
      "order",
    ]);

    const [rejected] = build(
      [newsOiFrameFixture({ event_id: "evt-oi-hype" })],
      tradingOrdersFixture({ orders: [] }),
    );
    const steps = leverageTimeline(rejected);
    expect(steps.map((step) => step.key)).toEqual(["trigger", "case", "strategy"]);
    expect(steps.map((step) => step.at)).toEqual([...steps.map((step) => step.at)].sort());
  });

  it("uses the case's own observation time when the frame is off the loaded page", () => {
    const [offPage] = build([], tradingOrdersFixture({ orders: [] }));

    expect(offPage.event).toBeUndefined();
    expect(leverageTimeline(offPage)[0].key).toBe("trigger");
  });
});

describe("parseLeverageTab", () => {
  it("falls back to the only tab whose contents can still change", () => {
    expect(parseLeverageTab(null)).toBe("live");
    expect(parseLeverageTab("nonsense")).toBe("live");
    expect(parseLeverageTab("done")).toBe("done");
    expect(Object.keys(LEVERAGE_TABS)).toEqual(["live", "directional", "no_trade", "done"]);
  });

  it("does not admit an inherited property as a tab", () => {
    /*
     * `value in LEVERAGE_TABS` walked the prototype chain, so `?lev=toString` satisfied the guard,
     * `LEVERAGE_TABS.toString.predicate` was `undefined`, and the route threw as soon as the lane held a
     * single case. A URL must never be able to blank a page.
     */
    const cases = build();
    for (const inherited of ["toString", "constructor", "valueOf", "hasOwnProperty", "__proto__"]) {
      const tab = parseLeverageTab(inherited);
      expect(tab).toBe("live");
      expect(() => leverageTabCount(cases, tab)).not.toThrow();
    }
  });
});
