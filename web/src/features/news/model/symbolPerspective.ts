import { REGIME_ZH, STRATEGY_ZH, policyRuleZh, type TradingOiLedgerEntry } from "@features/trading";

import type { NewsFeedEvent, NewsFeedOi } from "../api/newsQueries";

import { oiPercent, oiValueZh } from "./oiSignals";

/**
 * 交易视角 — how the capital lane read this token's most recent frame (#282, artifact v8).
 *
 * Three questions in the order the lane asks them: which quadrant the frame fell in, whether the move that
 * had already happened before it left room to enter, and where its two measurements sit against the floors
 * *this case* was decided against. Only the first is cheap; the other two are the reason this panel exists,
 * because the reading card's 利多 answers none of them.
 *
 * Everything is read off one case and one frame, and nothing is recomputed. In particular the thresholds
 * are the case's own frozen `strategy_config`, never today's configuration and never the operator settings
 * document: a frame refused under a 1000 bps ceiling that is 500 bps today would otherwise render as an
 * impossibility on screen (#269's lesson, and #273's).
 *
 * The artifact hard-codes a 1–6% band and calls the window 「帧前 1H」. Neither is a fact — they were the
 * thresholds and the window of the strategy running when it was drawn. Both come out of the case here, and
 * on the strategy running today they read 0–10% over five minutes.
 */

export type SymbolQuadrantCell = {
  active: boolean;
  code: string;
  key: string;
  label: string;
};

export type SymbolBandSegment = { flex: number; key: string; tone: "below" | "band" | "above" };

/** Where the measured move landed. Four placements because they are four different verdicts. */
export type SymbolBandPlacement = "above" | "below" | "in" | "unmeasured";

export type SymbolBand = {
  /** `帧前 5 分钟` — the strategy's own measurement window, not a round number. */
  caption: string;
  placement: SymbolBandPlacement;
  /** Where the measured move sits, 0–100 across the drawn domain. `null` when it was never measured. */
  markerPercent: number | null;
  measured: string;
  segments: SymbolBandSegment[];
  /**
   * Boundary labels with their own position across the domain, so a tick sits on the edge it names.
   *
   * `anchor` travels with the tick rather than being inferred from its position in the list. `spaced()`
   * returns two, three or four of these, so a stylesheet keying on `:first-child` anchored whichever tick
   * happened to be first — and for any pre-move in roughly (−228, 0) bps that is the `0.00%` *threshold*,
   * left-anchored 10.7px off the edge it names, inside the band instead of on its boundary.
   */
  ticks: ReadonlyArray<{ anchor: "start" | "middle" | "end"; label: string; percent: number }>;
};

export type SymbolFloorRow = {
  floor: string;
  key: string;
  label: string;
  measured: string;
  /**
   * Four answers, because an absent floor and an absent measurement are different facts and the card has
   * to name the one it means. `unset` is the case freezing no such floor; `unmeasured` is the floor being
   * frozen and the frame carrying nothing to compare it with.
   */
  verdict: "pass" | "fail" | "unmeasured" | "unset";
};

/**
 * Why the floor table has nothing to compare, when it has nothing.
 *
 * Five reasons, and they are not interchangeable. `no-join` is the common one: the server recovers a case's
 * `event_id` only when its source key round-trips as the deterministic OI contract, so a news- or
 * liquidation-triggered case publishes none by design. Saying 「那一帧不在这页里」 over one of those invents
 * a frame that was never named.
 */
type FloorSource = "read" | "not-a-frame" | "no-join" | "unloaded" | "unknown-strategy";

export type SymbolPerspective = {
  band: SymbolBand | null;
  floors: SymbolFloorRow[];
  /** What the floor table adds up to, in the reader's words. Derived — see `floorsNote`. */
  floorsNote: string;
  quadrants: SymbolQuadrantCell[];
  /**
   * The regime the lane assigned when it is not one of the four. `unclear` is a real and common answer —
   * it is what the band rule writes when the move is outside its own window — and four unlit quadrants
   * with no explanation reads as a failed render rather than as a verdict.
   */
  quadrantNote: string | null;
  /** The frame this reading is of, so the panel can name its own subject rather than implying "now". */
  frameAtMs: number | null;
  strategyId: string;
};

/** The four quadrants, in the artifact's reading order. Only the two buildup ones can ever open. */
const QUADRANTS: ReadonlyArray<{ code: string; label: string }> = [
  { code: "buildup_up", label: "增仓 · 价升" },
  { code: "buildup_down", label: "增仓 · 价跌" },
  { code: "deleveraging_up", label: "减仓 · 价升" },
  { code: "deleveraging_down", label: "减仓 · 价跌" },
];

/**
 * The lane's reading of one frame, or `null` when it never made one.
 *
 * A token with no case has no reading — not an empty one. The panel says so rather than drawing four grey
 * quadrants and a band with no marker, which reads as "we looked and found nothing" when the truth is that
 * the frame never reached a strategy.
 */
export function symbolPerspective(
  frame: NewsFeedEvent | undefined,
  entry: TradingOiLedgerEntry | undefined,
): SymbolPerspective | null {
  if (!entry) return null;
  const facts = perspectiveFacts(entry);
  const oi = frame?.oi;
  const floors = floorRows(oi, facts.config, facts.strategyId);
  return {
    band: bandOf(facts.config, facts.preMoveBps),
    floors,
    floorsNote: floorsNote(
      floors,
      floorSource(frame, facts.eventId, facts.strategyId),
      facts.strategyId,
      // The frame's own delivery, never an assumption — see `floorsNote`.
      frame?.delivery?.state === "sent",
    ),
    frameAtMs: frame?.opened_at_ms ?? null,
    quadrants: QUADRANTS.map((cell) => ({
      active: cell.code === facts.regime,
      code: cell.code,
      key: cell.code,
      label: REGIME_ZH[cell.code] ?? cell.label,
    })),
    quadrantNote: quadrantNote(facts.regime, facts.policyReason),
    strategyId: facts.strategyId,
  };
}

/** The four reasons `regime.assess()` reaches 象限不明 by, keyed exactly as the ledger writes them. */
const REGIME_UNCLEAR_REASONS = new Set([
  "move_above_band_chasing",
  "move_below_band",
  "no_price_fail_closed",
  "oi_direction_unknown",
]);

/**
 * What to say when the lane's regime is not one of the four cells above.
 *
 * `regime.assess()` reaches 象限不明 four ways and only one of them is 「price and OI did not align」: a
 * missing price, a move short of the band, a move past it, and an unreadable OI direction. Naming the
 * alignment one for all four contradicted the band card beside it — the smart-money lane leaves the shared
 * band at 600 and still goes long, so `unclear/move_above_band_chasing` sits next to 「落在带内」 routinely.
 */
function quadrantNote(regime: string | null, policyReason: string | null): string | null {
  if (regime == null) return "这条案例没有记录象限——判定停在更早的一步。";
  if (QUADRANTS.some((cell) => cell.code === regime)) return null;
  const label = REGIME_ZH[regime] ?? regime;
  return policyReason != null && REGIME_UNCLEAR_REASONS.has(policyReason)
    ? `这一帧被判为「${label}」，停在「${policyRuleZh(policyReason)}」——四个象限都不成立。`
    : `这一帧被判为「${label}」，四个象限都不成立；账本没有记下是哪一条判成这样的。`;
}

type PerspectiveFacts = {
  config: Record<string, string>;
  /** The frame the ledger published for this case, or `null` when it published none it could join. */
  eventId: string | null;
  /** The named rule the case stopped on — `null` while it is still PENDING or RUNNING. */
  policyReason: string | null;
  preMoveBps: number | null;
  regime: string | null;
  strategyId: string;
};

function perspectiveFacts(entry: TradingOiLedgerEntry): PerspectiveFacts {
  const value = entry.value;
  return {
    config: value.strategy_config ?? {},
    eventId: value.event_id ?? null,
    policyReason: value.policy_reason ?? null,
    preMoveBps: value.pre_move_bps ?? null,
    regime: value.regime ?? null,
    strategyId: value.strategy_id,
  };
}

/** A frozen threshold as a number, or `null` when the case froze none — never a plausible default. */
function frozen(config: Record<string, string>, key: string): number | null {
  const raw = config[key];
  if (raw == null) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * The move that had already happened before the frame, against the window the strategy would still enter.
 *
 * The drawn domain runs from `min(0, measured)` to comfortably past the ceiling, so the band occupies most
 * of the bar and the region beyond it — the one the research calls the chasing bucket — stays visible
 * rather than being clipped off the end.
 */
function bandOf(config: Record<string, string>, preMoveBps: number | null): SymbolBand | null {
  const min = frozen(config, "min_price_move_bps");
  const max = frozen(config, "max_price_move_bps");
  if (min == null || max == null || max <= min) return null;
  const windowMs = frozen(config, "measurement_window_ms");
  const low = Math.min(0, preMoveBps ?? 0);
  const high = Math.max(max * 1.4, preMoveBps ?? 0, max + 1);
  const span = high - low;
  const width = (from: number, to: number) => Math.max(0, ((to - from) / span) * 100);
  const at = (value: number) => ((value - low) / span) * 100;
  return {
    caption: windowMs == null ? "帧前已走行情" : `帧前${spanLabel(windowMs)}已走行情`,
    markerPercent: preMoveBps == null ? null : at(preMoveBps),
    measured: preMoveBps == null ? "未测量" : signedPercent(preMoveBps),
    placement:
      preMoveBps == null
        ? "unmeasured"
        : preMoveBps < min
          ? "below"
          : preMoveBps > max
            ? "above"
            : "in",
    segments: (
      [
        { flex: width(low, min), key: "below", tone: "below" },
        { flex: width(min, max), key: "band", tone: "band" },
        { flex: width(max, high), key: "above", tone: "above" },
      ] satisfies SymbolBandSegment[]
    ).filter((segment) => segment.flex > 0),
    /*
     * The two thresholds always; a domain end only when it is far enough from the threshold beside it to
     * be its own label. A frame that went down by 73 bps pulls the domain start to −0.73%, five percent of
     * the bar away from the 0.00% floor — two labels printed on top of each other, which is how this first
     * rendered.
     */
    ticks: spaced(
      { anchor: "start", label: oiPercent(low), percent: 0 },
      { anchor: "middle", label: oiPercent(min), percent: at(min) },
      { anchor: "middle", label: oiPercent(max), percent: at(max) },
      { anchor: "end", label: oiPercent(high), percent: 100 },
    ),
  };
}

/**
 * Drop any tick that would land on the one before it. The thresholds are the labels that matter, so the
 * domain ends yield: a bar with `−0.73%` and `0.00%` overlapping says less than one with just `0.00%`.
 *
 * The two thresholds can also collide with each other — the band is 5% of the bar once a pre-move runs to
 * +200%, which is the chasing population this card exists to explain — and neither may be dropped, since
 * between them they are the rule. They merge into one range label at the band's own midpoint instead.
 */
const TICK_MIN_GAP_PERCENT = 14;

type BandTick = { anchor: "start" | "middle" | "end"; label: string; percent: number };

function spaced(start: BandTick, min: BandTick, max: BandTick, end: BandTick): BandTick[] {
  const band: BandTick[] =
    max.percent - min.percent >= TICK_MIN_GAP_PERCENT
      ? [min, max]
      : [
          {
            anchor: "middle",
            label: `${min.label}–${max.label}`,
            percent: (min.percent + max.percent) / 2,
          },
        ];
  return [
    ...(min.percent - start.percent >= TICK_MIN_GAP_PERCENT ? [start] : []),
    ...band,
    ...(end.percent - max.percent >= TICK_MIN_GAP_PERCENT ? [end] : []),
  ];
}

/** `5 分钟` / `1 小时` — the strategy's window in the unit it was configured in. */
function spanLabel(spanMs: number): string {
  const minutes = Math.round(spanMs / 60_000);
  if (minutes < 60) return ` ${minutes} 分钟`;
  return ` ${Math.round(minutes / 60)} 小时`;
}

function signedPercent(bps: number): string {
  return `${bps > 0 ? "+" : bps < 0 ? "−" : ""}${oiPercent(Math.abs(bps))}`;
}

/**
 * Which floors each strategy freezes, and how it compares each one.
 *
 * Inclusivity is per key and per strategy, and the lane treats it as load-bearing.
 * `oi_smart_money_momentum.py` refuses `whale_oi_ratio_bps <= floor` and `whale_long_profit_bps <= floor`
 * — strictly greater to pass, 「not negotiable」 in its own docstring — while its `min_oi_change_bps`
 * refuses `< floor`, and `oi_momentum_v1` / `news_oi_alignment_v1` reach the profit floor through the
 * shared `oi_gate`, which refuses on `<`. A table that read `>=` everywhere stamped 过地板 on exactly the
 * frames the ledger refused: the shipped smart-money profit floor is 0, and 0 is a refusal there.
 *
 * Keyed by `strategy_id` for the same reason. `/api/trading/orders?underlying=` filters on the name alone,
 * so the newest case can belong to any lane, and the three freeze disjoint `strategy_config` key sets —
 * one lane's rows over another lane's case renders every row 未冻结 and explains nothing.
 */
type FloorRule = {
  compare: "gt" | "gte";
  configKey: string;
  key: string;
  label: string;
  read: (oi: NewsFeedOi) => number | null | undefined;
};

function profitFloor(compare: "gt" | "gte"): FloorRule {
  return {
    compare,
    configKey: "min_whale_long_profit_bps",
    key: "profit",
    label: "鲸鱼盈利",
    read: (oi) => oi.whale_long_profit_bps,
  };
}

const STRATEGY_FLOORS: Record<string, ReadonlyArray<FloorRule>> = {
  news_oi_alignment_v1: [profitFloor("gte")],
  oi_momentum_v1: [profitFloor("gte")],
  oi_smart_money_momentum_v1: [
    profitFloor("gt"),
    {
      compare: "gt",
      configKey: "min_whale_oi_ratio_bps",
      key: "ratio",
      label: "大户占比",
      read: (oi) => oi.whale_oi_ratio_bps,
    },
    {
      compare: "gte",
      configKey: "min_oi_change_bps",
      key: "change",
      label: "持仓增幅",
      read: (oi) => oi.oi_change_bps,
    },
  ],
};

/**
 * The frame's Alpha measurements against the floors this case was decided against.
 *
 * A floor the case never froze is `unset`, not a pass: the console is sometimes newer than the ledger row
 * it is describing, and `measured >= 0` would stamp 过地板 on every frame ever written. A floor that *was*
 * frozen over a frame carrying no such measurement is `unmeasured` — a different fact, and calling it
 * 未冻结 printed 「未冻结」 beside the very threshold the case had frozen.
 */
function floorRows(
  oi: NewsFeedOi | null | undefined,
  config: Record<string, string>,
  strategyId: string,
): SymbolFloorRow[] {
  const rules = STRATEGY_FLOORS[strategyId];
  if (rules == null) return [];
  return [
    ...rules.map((rule) =>
      row(rule, oi == null ? null : (rule.read(oi) ?? null), frozen(config, rule.configKey)),
    ),
    {
      floor: "—",
      key: "value",
      label: "持仓规模",
      measured: oiValueZh(oi?.oi_value_usd),
      // The size floor moved to the Candidate Gate with its own digest (#264); none of these strategies
      // freezes one, and claiming one here would name a threshold the case was not measured against.
      verdict: "unset",
    },
  ];
}

/** Which of the five reasons applies, from the three facts that decide it. */
function floorSource(
  frame: NewsFeedEvent | undefined,
  eventId: string | null,
  strategyId: string,
): FloorSource {
  if (STRATEGY_FLOORS[strategyId] == null) return "unknown-strategy";
  if (frame != null) return frame.oi == null ? "not-a-frame" : "read";
  return eventId == null ? "no-join" : "unloaded";
}

/**
 * What the floor table adds up to.
 *
 * Derived rather than written down, because 「一条都不过」 is itself a measurement. The card printed that
 * sentence under four rows whose measurements had never been read, which announces a verdict the frame
 * never received — and having nothing to compare is the state the console lands in most often, since most
 * cases publish no joinable `event_id` at all.
 *
 * `pushed` is the frame's own delivery, not an assumption. The reader gate and the capital lane run
 * different thresholds in the same direction — `trade_projection.py` dropped `final_decision IN
 * ('push','escalate')` from the OI projection precisely so the lane could see frames the reader withheld —
 * so 「这一帧推送了」 over a withheld frame states the opposite of what the ledger recorded.
 */
function floorsNote(
  rows: SymbolFloorRow[],
  source: FloorSource,
  strategyId: string,
  pushed: boolean,
): string {
  const compared = rows.filter((row) => row.verdict === "pass" || row.verdict === "fail");
  if (compared.length === 0) {
    if (source !== "unknown-strategy") return UNCOMPARED_ZH[source];
    const label = STRATEGY_ZH[strategyId] ?? strategyId;
    return `这条案例由「${label}」判的，控制台没有记下它冻结哪几条地板——不替它猜。`;
  }
  const passed = compared.filter((row) => row.verdict === "pass").length;
  const tail =
    passed === 0
      ? `在这里 ${compared.length} 条一条都没过`
      : passed === compared.length
        ? `在这里 ${compared.length} 条全过了`
        : `在这里 ${compared.length} 条只过了 ${passed} 条`;
  return pushed
    ? `推送闸门和交易地板是两套阈值：这一帧推送了，${tail}。`
    : `这一帧没有推送，资本通道照样读了它——两套阈值互不代表，${tail}。`;
}

const UNCOMPARED_ZH: Record<Exclude<FloorSource, "unknown-strategy">, string> = {
  "no-join":
    "这条案例不是确定性 OI 帧开出的，账本没有发布可连的 event_id——测量值无从取，地板比不了。",
  "not-a-frame": "开出这条案例的不是一条 OI 帧，这些测量值它没有——地板比不了，不是没过。",
  read: "这一帧没有留下这些测量值——地板比不了，不是没过。",
  unloaded: "开出这条案例的那一帧不在下面这段窗口里，测量值取不到——地板是案上冻结的，比不了。",
};

function row(rule: FloorRule, measured: number | null, floor: number | null): SymbolFloorRow {
  return {
    // The operator is the strategy's, printed as the strategy wrote it: `>` is not a rounder `≥`.
    floor: floor == null ? "—" : `${rule.compare === "gt" ? ">" : "≥"} ${oiPercent(floor)}`,
    key: rule.key,
    label: rule.label,
    measured: oiPercent(measured),
    verdict:
      floor == null
        ? "unset"
        : measured == null
          ? "unmeasured"
          : passes(rule.compare, measured, floor)
            ? "pass"
            : "fail",
  };
}

function passes(compare: "gt" | "gte", measured: number, floor: number): boolean {
  return compare === "gt" ? measured > floor : measured >= floor;
}
