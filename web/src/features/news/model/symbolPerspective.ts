import { REGIME_ZH, type TradingOiLedgerEntry } from "@features/trading";

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
  /** Boundary labels with their own position across the domain, so a tick sits on the edge it names. */
  ticks: ReadonlyArray<{ label: string; percent: number }>;
};

export type SymbolFloorRow = {
  bucket: string | null;
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
 * Four reasons, and they are not interchangeable. `no-join` is the common one: the server recovers a case's
 * `event_id` only when its source key round-trips as the deterministic OI contract, so a news- or
 * liquidation-triggered case publishes none by design. Saying 「那一帧不在这页里」 over one of those invents
 * a frame that was never named.
 */
type FloorSource = "read" | "not-a-frame" | "no-join" | "unloaded";

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
  const floors = floorRows(oi, facts.config);
  return {
    band: bandOf(facts.config, facts.preMoveBps),
    floors,
    floorsNote: floorsNote(floors, floorSource(frame, facts.eventId)),
    frameAtMs: frame?.opened_at_ms ?? null,
    quadrants: QUADRANTS.map((cell) => ({
      active: cell.code === facts.regime,
      code: cell.code,
      key: cell.code,
      label: REGIME_ZH[cell.code] ?? cell.label,
    })),
    quadrantNote: quadrantNote(facts.regime),
    strategyId: facts.strategyId,
  };
}

/** What to say when the lane's regime is not one of the four cells above. */
function quadrantNote(regime: string | null): string | null {
  if (regime == null) return "这条案例没有记录象限——判定停在更早的一步。";
  if (QUADRANTS.some((cell) => cell.code === regime)) return null;
  const label = REGIME_ZH[regime] ?? regime;
  return `这一帧被判为「${label}」，四个象限都不成立：价格与持仓没有同向到可以定方向。`;
}

type PerspectiveFacts = {
  config: Record<string, string>;
  /** The frame the ledger published for this case, or `null` when it published none it could join. */
  eventId: string | null;
  preMoveBps: number | null;
  regime: string | null;
  strategyId: string;
};

function perspectiveFacts(entry: TradingOiLedgerEntry): PerspectiveFacts {
  const value = entry.value;
  return {
    config: value.strategy_config ?? {},
    eventId: value.event_id ?? null,
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
    ticks: spaced([
      { label: oiPercent(low), percent: 0 },
      { label: oiPercent(min), percent: at(min) },
      { label: oiPercent(max), percent: at(max) },
      { label: oiPercent(high), percent: 100 },
    ]),
  };
}

/**
 * Drop any tick that would land on the one before it. The thresholds are the labels that matter, so the
 * domain ends yield: a bar with `−0.73%` and `0.00%` overlapping says less than one with just `0.00%`.
 */
const TICK_MIN_GAP_PERCENT = 14;

function spaced(
  ticks: ReadonlyArray<{ label: string; percent: number }>,
): ReadonlyArray<{ label: string; percent: number }> {
  const [start, min, max, end] = ticks;
  return [
    ...(min.percent - start.percent >= TICK_MIN_GAP_PERCENT ? [start] : []),
    min,
    max,
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
 * The frame's two Alpha measurements against the floors this case was decided against.
 *
 * A floor the case never froze is `unset`, not a pass: the console is sometimes newer than the ledger row
 * it is describing, and `measured >= 0` would stamp 过地板 on every frame ever written. A floor that *was*
 * frozen over a frame carrying no such measurement is `unmeasured` — a different fact, and calling it
 * 未冻结 printed 「未冻结」 beside the very threshold the case had frozen.
 */
function floorRows(
  oi: NewsFeedOi | null | undefined,
  config: Record<string, string>,
): SymbolFloorRow[] {
  const profitFloor = frozen(config, "min_whale_long_profit_bps");
  const ratioFloor = frozen(config, "min_whale_oi_ratio_bps");
  const changeFloor = frozen(config, "min_oi_change_bps");
  return [
    row("profit", "鲸鱼盈利", oi?.whale_long_profit_bps ?? null, profitFloor, oiPercent),
    row("ratio", "大户占比", oi?.whale_oi_ratio_bps ?? null, ratioFloor, oiPercent),
    row("change", "持仓增幅", oi?.oi_change_bps ?? null, changeFloor, oiPercent),
    {
      bucket: null,
      floor: "—",
      key: "value",
      label: "持仓规模",
      measured: oiValueZh(oi?.oi_value_usd),
      // The size floor moved to the Candidate Gate with its own digest (#264); this strategy freezes none,
      // and claiming one here would name a threshold the case was not measured against.
      verdict: "unset",
    },
  ];
}

/** Which of the four reasons applies, from the two facts that decide it. */
function floorSource(frame: NewsFeedEvent | undefined, eventId: string | null): FloorSource {
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
 */
function floorsNote(rows: SymbolFloorRow[], source: FloorSource): string {
  const compared = rows.filter((row) => row.verdict === "pass" || row.verdict === "fail");
  if (compared.length === 0) return UNCOMPARED_ZH[source];
  const passed = compared.filter((row) => row.verdict === "pass").length;
  const tail =
    passed === 0
      ? `在这里 ${compared.length} 条一条都没过`
      : passed === compared.length
        ? `在这里 ${compared.length} 条全过了`
        : `在这里 ${compared.length} 条只过了 ${passed} 条`;
  return `推送闸门和交易地板是两套阈值：这一帧可以推送了，${tail}。`;
}

const UNCOMPARED_ZH: Record<FloorSource, string> = {
  "no-join":
    "这条案例不是确定性 OI 帧开出的，账本没有发布可连的 event_id——测量值无从取，地板比不了。",
  "not-a-frame": "开出这条案例的不是一条 OI 帧，这三项测量值它没有——地板比不了，不是没过。",
  read: "这一帧没有留下这三项测量值——地板比不了，不是没过。",
  unloaded: "开出这条案例的那一帧不在下面这段窗口里，测量值取不到——地板是案上冻结的，比不了。",
};

function row(
  key: string,
  label: string,
  measured: number | null,
  floor: number | null,
  format: (value: number | null | undefined) => string,
): SymbolFloorRow {
  return {
    bucket: null,
    floor: floor == null ? "—" : `≥ ${format(floor)}`,
    key,
    label,
    measured: format(measured),
    verdict:
      floor == null
        ? "unset"
        : measured == null
          ? "unmeasured"
          : measured >= floor
            ? "pass"
            : "fail",
  };
}
