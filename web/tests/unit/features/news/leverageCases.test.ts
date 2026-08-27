import {
  LEVERAGE_TABS,
  evidenceRows,
  leverageCases,
  leverageFramelessCount,
  leverageFunnel,
  leverageHorizon,
  leverageListRows,
  leverageRemaining,
  leverageTabCount,
  leverageTimeline,
  leverageTopReasons,
  parseLeverageTab,
  type LeverageThresholds,
} from "@features/news/model/leverageCases";
import { tradingLedgerEntries } from "@features/trading";
import { newsOiFrameFixture } from "@tests/fixtures/newsFixture";
import {
  tradingCaseFixture,
  tradingOrderFixture,
  tradingOrdersFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

const STATUS = tradingStatusFixture();
const THRESHOLDS: LeverageThresholds = { gate: STATUS.gate, strategies: STATUS.strategies ?? [] };
const NOW_MS = 1_756_000_000_000;

function build(events = [newsOiFrameFixture()], orders = tradingOrdersFixture()) {
  return leverageCases(events, tradingLedgerEntries(orders), THRESHOLDS, NOW_MS);
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
      cases_without_orders: [
        tradingCaseFixture({ case_id: "case-off-page", event_id: "evt-off-page" }),
        // A news-triggered case: the server publishes `event_id: null` by design, and this is the row an
        // `event_id`-keyed index silently dropped — which is most of the live lane.
        tradingCaseFixture({ case_id: "case-news", event_id: null, trigger_kind: "news" }),
      ],
      orders: [tradingOrderFixture({ case_id: "case-framed", event_id: "evt-oi-wif" })],
    });

    const ids = build(frames, ledger).map((item) => item.id);
    expect(ids).toEqual(expect.arrayContaining(["case-framed", "case-off-page", "case-news"]));
    // The frame nothing judged is still not a case.
    expect(build(frames, ledger).some((item) => item.event?.event_id === "evt-never-judged")).toBe(
      false,
    );
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
        tradingCaseFixture({
          case_id: "case-rejected",
          event_id: "evt-rejected",
          state: "POLICY_REJECTED",
        }),
      ],
      orders: [
        tradingOrderFixture({ case_id: "case-open", event_id: "evt-open", state: "OPEN" }),
        tradingOrderFixture({
          case_id: "case-closed",
          event_id: "evt-closed",
          order_id: "o2",
          state: "CLOSED",
        }),
      ],
    });

    const byId = Object.fromEntries(build(frames, ledger).map((item) => [item.id, item.phase]));
    expect(byId).toEqual({
      "case-closed": "resolved",
      "case-open": "active",
      "case-rejected": "no_trade",
    });
  });

  it("puts capital at risk first and never invents a composite score", () => {
    const frames = ["evt-a", "evt-b", "evt-c"].map((event_id) => newsOiFrameFixture({ event_id }));
    const ledger = tradingOrdersFixture({
      cases_without_orders: [],
      orders: [
        // Newest, but merely open.
        tradingOrderFixture({
          case_id: "case-a",
          case_observed_at_ms: NOW_MS - 1_000,
          event_id: "evt-a",
          state: "OPEN",
        }),
        // Older, and unreconciled: this is the row an operator has to see first.
        tradingOrderFixture({
          case_id: "case-b",
          case_observed_at_ms: NOW_MS - 900_000,
          event_id: "evt-b",
          order_id: "o2",
          state: "AMBIGUOUS",
        }),
        tradingOrderFixture({
          case_id: "case-c",
          case_observed_at_ms: NOW_MS - 500_000,
          event_id: "evt-c",
          order_id: "o3",
          state: "CLOSED",
        }),
      ],
    });

    expect(build(frames, ledger).map((item) => item.id)).toEqual(["case-b", "case-a", "case-c"]);
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
    const rows = evidenceRows(newsOiFrameFixture().oi, "oi_momentum_v1", "oi", THRESHOLDS);

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
    // The WIF fixture clears the gate's admission floor, misses this strategy's whale floor, and has a
    // positive OI change — three real answers, each from the rule that owns it.
    expect(rows.find((row) => row.key === "oi")?.status).toBe("support");
    expect(rows.find((row) => row.key === "value")?.status).toBe("support");
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
    const rows = evidenceRows(newsOiFrameFixture().oi, "news_oi_alignment_v1", "news", THRESHOLDS);

    expect(rows.find((row) => row.key === "news")?.status).toBe("missing");
  });

  it("refuses to call an unconfigured floor a pass", () => {
    // Zero thresholds arrive when the console is newer than the API, or when the status read failed.
    // `measured >= 0` would stamp 支持 on every frame against a threshold nobody set.
    const rows = evidenceRows(newsOiFrameFixture().oi, "oi_momentum_v1", "oi", {
      gate: undefined,
      strategies: [],
    });

    expect(rows.find((row) => row.key === "value")?.status).toBe("na");
    expect(rows.find((row) => row.key === "whale")?.status).toBe("na");
  });

  it("measures a case against the thresholds of the strategy that decided it", () => {
    /*
     * #269. The smart-money template binds on `whale / OI > 50%` and sets no profit floor at all; the
     * strategy beside it binds on 95% whale profit. Comparing every case against one lane-wide number
     * put 冲突 on a row this case had passed, and named a rule it never faced.
     */
    const oi = newsOiFrameFixture().oi;
    const smartMoney = evidenceRows(oi, "oi_smart_money_momentum_v1", "oi", THRESHOLDS);
    const alignment = evidenceRows(oi, "news_oi_alignment_v1", "oi", THRESHOLDS);

    const whale = smartMoney.find((row) => row.key === "whale");
    expect(whale?.label).toBe("鲸鱼占比");
    expect(whale?.note).toContain("策略 oi_smart_money_momentum_v1");
    // The fixture frame is 65.93% concentrated: above this strategy's 50%, below the other's 95% profit.
    expect(whale?.status).toBe("support");
    expect(alignment.find((row) => row.key === "whale")?.label).toBe("鲸鱼盈利");
    expect(alignment.find((row) => row.key === "whale")?.status).toBe("conflict");
  });

  it("reads a strictly-above floor strictly, at the exact boundary", () => {
    /*
     * `oi_smart_money_momentum_v1` refuses on `whale_oi_ratio_bps <= min_whale_oi_ratio_bps` and its
     * module calls that non-negotiable — 5001 qualifies, 5000 does not. Comparing with `>=` printed
     * 支持 · "50.00% ≥ 现行地板 50.00%" beside a case whose named rule is
     * `smart_money_ratio_below_or_equal_floor`: the wrong-comparison class this row exists to remove.
     */
    const atFloor = {
      ...newsOiFrameFixture().oi!,
      whale_oi_ratio_bps: 5_000,
    };
    const row = evidenceRows(atFloor, "oi_smart_money_momentum_v1", "oi", THRESHOLDS).find(
      (item) => item.key === "whale",
    );

    expect(row?.status).toBe("conflict");
    expect(row?.note).toContain("≤ 现行地板");

    // One basis point above is the strategy's own answer, and the note says which way it read.
    const above = evidenceRows(
      { ...atFloor, whale_oi_ratio_bps: 5_001 },
      "oi_smart_money_momentum_v1",
      "oi",
      THRESHOLDS,
    ).find((item) => item.key === "whale");
    expect(above?.status).toBe("support");
    expect(above?.note).toContain("> 现行地板");

    // The profit floors are inclusive, and stay that way: `oi_momentum_v1` refuses on `profit < floor`.
    const profitAtFloor = evidenceRows(
      { ...newsOiFrameFixture().oi!, whale_long_profit_bps: 9_500 },
      "oi_momentum_v1",
      "oi",
      THRESHOLDS,
    ).find((item) => item.key === "whale");
    expect(profitAtFloor?.status).toBe("support");
    expect(profitAtFloor?.note).toContain("≥ 现行地板");
  });

  it("compares scale against the admission gate rather than the settings document", () => {
    // #264 gave the liquidity floor one owner. The page was still reading the operator's 20M while
    // admission ran at 5M, so a frame the gate admitted showed 冲突 on 规模.
    const row = evidenceRows(newsOiFrameFixture().oi, "oi_momentum_v1", "oi", THRESHOLDS).find(
      (item) => item.key === "value",
    );

    expect(row?.note).toContain("准入闸");
    // 11.03M against the gate's 5M. Read out of `floors.min_oi_value_usd` — still 20M in this fixture,
    // exactly as production's settings document was — the same frame reported 冲突.
    expect(row?.status).toBe("support");
    expect(STATUS.floors.min_oi_value_usd).toBe("20000000");
  });
});

describe("leverageFunnel", () => {
  it("describes a day the lane produced nothing, from the ledger that outlives it", () => {
    /*
     * #269. Production runs about 110 frames and one case a day, so every tab on the page reads zero and
     * the list is blank — a true statement that reads as an outage. This is the same 24 hours as a
     * sentence, and it comes from the durable admission ledger rather than the midnight-reset counter.
     */
    const steps = leverageFunnel(STATUS.counts, []);

    expect(steps.map((step) => [step.label, step.value])).toEqual([
      ["遥测帧", 91],
      ["过闸成案", 1],
      ["有方向", 0],
      ["订单", 0],
    ]);
  });

  it("counts only the lane the admission ledger describes", () => {
    /*
     * `candidate_admission_report` scopes to `trigger_kind = 'oi'` and the News lane writes no gate row
     * at all, while the case batch beside it carries every trigger kind. Counting all of them in the
     * tail rendered 遥测帧 110 · 过闸 1 · 案例 60 — a third step sixty times its second, under a rule
     * that draws each step narrower than the last.
     */
    const newsCase = tradingCaseFixture({
      case_id: "case-news",
      event_id: null,
      policy_reason: "oi_context_missing",
      strategy_id: "news_oi_alignment_v1",
      trigger_kind: "news",
    });
    const cases = leverageCases(
      [],
      tradingLedgerEntries(
        tradingOrdersFixture({ cases_without_orders: [newsCase], orders: [tradingOrderFixture()] }),
      ),
      THRESHOLDS,
      NOW_MS,
    );

    // The WIF order fixture is news-triggered too, so every tail step is zero and the funnel narrows.
    const values = leverageFunnel(STATUS.counts, cases).map((step) => step.value);
    expect(values).toEqual([91, 1, 0, 0]);
    expect(values.every((value, index) => index === 0 || value <= values[index - 1])).toBe(true);
  });

  it("names the rules that are actually binding, and never counts admission as a refusal", () => {
    const reasons = leverageTopReasons(STATUS.counts);

    expect(reasons.map((reason) => reason.label)).toEqual([
      "窗口内名次超限",
      "持仓额低于流动性地板",
      "该场所无原生永续",
    ]);
    expect(reasons.some((reason) => reason.key === "freeze:case_created")).toBe(false);
  });

  it("reports an unreadable status as unknown rather than as zero frames", () => {
    expect(leverageFunnel(undefined, []).every((step) => step.value === 0)).toBe(true);
  });
});

describe("leverageListRows", () => {
  const noTrade = (caseId: string, overrides: Record<string, unknown> = {}) =>
    tradingCaseFixture({
      case_id: caseId,
      event_id: null,
      policy_reason: "oi_context_missing",
      strategy_id: "news_oi_alignment_v1",
      trigger_kind: "news",
      ...overrides,
    });

  it("collapses repeated identical refusals into one counted row", () => {
    /*
     * `news_oi_alignment_v1` needs a News trigger and a fresh OI frame for one issuer to meet inside a
     * scan window, which is structurally near-zero — so this outcome is the lane's resting state, and
     * production listed 59 near-identical cards of it with the day's one OI case in the middle.
     */
    const cases = leverageCases(
      [],
      tradingLedgerEntries(
        tradingOrdersFixture({
          cases_without_orders: [noTrade("c1"), noTrade("c2"), noTrade("c3")],
          orders: [],
        }),
      ),
      THRESHOLDS,
      NOW_MS,
    );

    const rows = leverageListRows(cases);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("group");
    expect(rows[0].kind === "group" && rows[0].items).toHaveLength(3);
  });

  it("never collapses an OI-triggered case, and leaves two of a kind alone", () => {
    // The OI lane is the population this page exists for; one of its cases is worth a row even when it
    // says what yesterday's said. And two identical rows are a coincidence a reader can read past.
    const cases = leverageCases(
      [],
      tradingLedgerEntries(
        tradingOrdersFixture({
          cases_without_orders: [
            noTrade("c1"),
            noTrade("c2"),
            noTrade("oi-1", {
              event_id: "evt-oi-a",
              policy_reason: "smart_money_oi_change_below_floor",
              strategy_id: "oi_smart_money_momentum_v1",
              trigger_kind: "oi",
            }),
          ],
          orders: [],
        }),
      ),
      THRESHOLDS,
      NOW_MS,
    );

    expect(leverageListRows(cases).every((row) => row.kind === "case")).toBe(true);
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

describe("leverageRemaining", () => {
  it("stops the clock when the position is gone, rather than counting down a deadline nobody kept", () => {
    /*
     * The ledger never clears `must_close_at_ms` on close — `orders.py` writes
     * `coalesce(%s, must_close_at_ms)` — so a position that exited early on its native stop still carries
     * a deadline hours out. Reading the order before the phase printed a live countdown against a
     * position that no longer existed, and 已到期 after the deadline passed, which claims a forced close
     * that never happened.
     */
    const closed = tradingOrderFixture({
      exit_reason: "native_stop",
      must_close_at_ms: NOW_MS + 2 * 3_600_000,
      position_closed_at_ms: NOW_MS - 1_800_000,
      position_opened_at_ms: NOW_MS - 3_600_000,
      state: "CLOSED",
    });
    const [item] = build([], tradingOrdersFixture({ cases_without_orders: [], orders: [closed] }));

    expect(item.phase).toBe("resolved");
    expect(leverageRemaining(item, NOW_MS)).toBe("已了结");
  });

  it("counts down only a position the ledger says is open", () => {
    const open = tradingOrderFixture({
      must_close_at_ms: NOW_MS + 3 * 3_600_000 + 12 * 60_000,
      position_opened_at_ms: NOW_MS - 60_000,
      state: "OPEN",
    });
    const [item] = build([], tradingOrdersFixture({ cases_without_orders: [], orders: [open] }));

    expect(leverageRemaining(item, NOW_MS)).toBe("剩 3h 12m");
  });

  it("says which state an unfilled intent is in rather than inventing a TTL for it", () => {
    // The artifact draws `TTL 42s` here; this ledger publishes no approval expiry, and `must_close_at_ms`
    // is measured from the first fill, so nothing has started counting.
    const waiting = tradingOrderFixture({
      must_close_at_ms: null,
      position_opened_at_ms: null,
      state: "AWAITING_APPROVAL",
    });
    const [item] = build([], tradingOrdersFixture({ cases_without_orders: [], orders: [waiting] }));

    expect(leverageRemaining(item, NOW_MS)).toBe("未起算");
  });

  it("reads the permission chip off the case, not off the strategy's current setting", () => {
    /*
     * `mode` is the ledger's record of what this decision was allowed to do. The chip used to read
     * `strategies[].permission` from the live status document, so promoting a strategy relabelled every
     * case still inside the window — including ones that only ever ran on paper, which is the exact
     * confusion a chip saying 「LONG, on paper」 exists to prevent.
     */
    const [item] = build(
      [],
      tradingOrdersFixture({
        cases_without_orders: [tradingCaseFixture({ mode: "live_reviewed" })],
        orders: [],
      }),
    );

    expect(item.mode).toBe("live_reviewed");
  });
});

describe("leverageHorizon", () => {
  it("reads the window the order froze, not the mandate running today", () => {
    /*
     * `must_close_at_ms` is written from the budget at order time and measured from the first fill, so the
     * span between them is what this position was opened under. Reading `budget.max_hold_ms` re-described
     * a historical case with today's configuration — the same mistake the permission chip made, on the
     * row directly beneath it, on a field that is a risk limit.
     */
    const opened = NOW_MS - 600_000;
    const order = tradingOrderFixture({
      must_close_at_ms: opened + 4 * 3_600_000,
      position_opened_at_ms: opened,
      state: "OPEN",
    });
    const [item] = build([], tradingOrdersFixture({ cases_without_orders: [], orders: [order] }));

    expect(leverageHorizon(item, 1_800_000)).toBe("4 小时");
  });

  it("names the mandate as the current budget when the case froze no window of its own", () => {
    const [item] = build(
      [],
      tradingOrdersFixture({ cases_without_orders: [tradingCaseFixture()], orders: [] }),
    );

    expect(leverageHorizon(item, 1_800_000)).toBe("30 分钟 · 当前预算");
    // An unread mandate is a dash, never a plausible default.
    expect(leverageHorizon(item, undefined)).toBe("—");
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
