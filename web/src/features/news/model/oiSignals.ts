import {
  NEWS_OI_TABS,
  type NewsFeedOi,
  type NewsOiTab,
  type NewsOiTradeFloors,
} from "../api/newsQueries";

/**
 * Display helpers for #137's deterministic open-interest lane (#207).
 *
 * Nothing here judges. Every gate name, threshold, measurement and rank on this page is a server value read
 * back from the trace `oi_judgment_trace()` wrote; this module turns integers into characters and picks
 * which Chinese word names a key the server already chose. Re-deriving any of it from `leader_title` would
 * be `oi_signal_parser_v1` running a second time in the browser, drifting from the judge the moment either
 * side changed.
 */

/** The judge's own rule keys. The console shows the key beside its Chinese name; it never replaces it. */
export const OI_PUSH_RULE = "opening_move_with_whale_concentration";
export const OI_PARSE_FAILED_RULE = "oi_parse_failed";
export const OI_WITHHELD_RULES = [
  "whale_ratio_below_threshold",
  "oi_change_below_threshold",
  "beyond_window_rank",
] as const;

const OI_RULE_ZH: Record<string, string> = {
  [OI_PUSH_RULE]: "开仓异动且鲸鱼集中",
  whale_ratio_below_threshold: "鲸鱼占比未过阈值",
  oi_change_below_threshold: "持仓变动未过下限",
  beyond_window_rank: "窗口名次已用满",
  [OI_PARSE_FAILED_RULE]: "供应商格式无法解析",
};

export function oiRuleLabel(rule: string | null | undefined): string {
  const key = String(rule ?? "");
  return OI_RULE_ZH[key] ?? key;
}

export const OI_TAB_LABELS: Record<NewsOiTab, string> = {
  all: "全部",
  pushed: "已推送",
  withheld: "未达阈值",
  parse_failed: "解析失败",
};

/** An unknown `?oi=` falls back to the whole lane rather than a 4xx, the way the feed's filters do. */
export function parseOiTab(value: string | null): NewsOiTab {
  return NEWS_OI_TABS.find((tab) => tab === value) ?? "all";
}

/**
 * Each tab's count. Three server fields, and they do not all count the same thing on purpose.
 *
 * The three judged tabs come from `oi.by_rule_24h`, keyed on the gate that decided. `all` cannot: those
 * buckets hold *judged verdicts*, and a frame that arrived and has not been judged yet renders as a row —
 * with no `oi` block — while belonging to none of them, so summing them would advertise fewer than the tab
 * can reach. Nor is `all` `telemetry_received_24h`, which counts provider items *before* the Gate and so
 * names frames no row can ever show. It is `telemetry_events_24h`: Events on this admission, which is the
 * table's own universe. `received` keeps its tile in the band above, where it is the lane's intake.
 */
export function oiTabCount(
  tab: NewsOiTab,
  byRule: Record<string, number> | undefined,
  events24h: number | undefined,
): number | null {
  if (tab === "all") return events24h ?? null;
  if (!byRule) return null;
  if (tab === "pushed") return byRule[OI_PUSH_RULE] ?? 0;
  if (tab === "parse_failed") return byRule[OI_PARSE_FAILED_RULE] ?? 0;
  return OI_WITHHELD_RULES.reduce((total, rule) => total + (byRule[rule] ?? 0), 0);
}

/**
 * Basis points as a percentage.
 *
 * Unsigned on purpose: every value this renders — a whale ratio, a profit, a threshold — is a magnitude,
 * and the one measurement whose sign carries meaning is the OI change, which `oiChangeLabel` writes itself.
 */
export function oiPercent(bps: number | null | undefined, digits = 2): string {
  if (bps == null || !Number.isFinite(bps)) return "—";
  return `${(bps / 100).toFixed(digits)}%`;
}

/**
 * Open interest in the units the reader card already uses: `32_170_000` -> `3217 万`.
 *
 * `_usd_zh` on the server is what the pushed headline carries, and the monitor must not disagree with the
 * card a reader received about how much open interest a frame reported. So this is that function's integer
 * arithmetic transcribed, not an approximation of it: `Math.round` is half-up where Python's `:.0f` is
 * half-to-even, which would put `2.5 万` on the card and `3 万` here for the same frame.
 */
export function oiValueZh(usd: number | null | undefined): string {
  if (usd == null || !Number.isFinite(usd)) return "—";
  const amount = Math.trunc(usd);
  if (amount >= 100_000_000) {
    const hundredths = Math.floor((amount * 100 + 50_000_000) / 100_000_000);
    return `${Math.floor(hundredths / 100)}.${String(hundredths % 100).padStart(2, "0")} 亿`;
  }
  if (amount >= 10_000) return `${halfToEven(amount, 10_000)} 万`;
  return String(amount);
}

/** Python's `:.0f` rounding: ties go to the even neighbour, not away from zero. */
function halfToEven(value: number, divisor: number): number {
  const whole = Math.floor(value / divisor);
  const remainder = value - whole * divisor;
  if (remainder * 2 > divisor) return whole + 1;
  if (remainder * 2 < divisor) return whole;
  return whole % 2 === 0 ? whole : whole + 1;
}

/**
 * The OI move with the direction spelled out by its own sign.
 *
 * `oi_change_bps` is signed on the trace, so `−` here is open interest falling — never a price call. Which
 * way the price went is a different measurement and lives in the 1H / 4H columns.
 */
export function oiChangeLabel(oi: NewsFeedOi | null | undefined): string {
  if (!oi?.parsed || oi.oi_change_bps == null) return "—";
  const pct = Math.abs(oi.oi_change_bps) / 100;
  return `${oi.oi_change_bps < 0 ? "−" : "+"}${pct.toFixed(2)}%`;
}

/**
 * `1 / 2` — the slot this frame spent — or `—` when it spent none.
 *
 * `evaluate_oi` computes `rank = earlier_eligible_count + 1` for *every* frame and the trace records it
 * unconditionally, so a frame the whale or OI-change threshold rejected still carries a number. That number
 * is the rank it would have taken had it qualified: it never became eligible and never consumed a slot, and
 * printing `1 / 2` beside it would say the window is fuller than it is — exactly the reading the window
 * occupancy card exists to get right.
 */
const OI_INELIGIBLE_RULES = new Set(["whale_ratio_below_threshold", "oi_change_below_threshold"]);

export function oiRankLabel(oi: NewsFeedOi | null | undefined): string {
  if (oi?.eligible_rank_in_window == null || OI_INELIGIBLE_RULES.has(oi.rule)) return "—";
  const max = oi.max_rank_in_window;
  return max == null
    ? String(oi.eligible_rank_in_window)
    : `${oi.eligible_rank_in_window} / ${max}`;
}

/**
 * The rank window as the operator configured it, exactly — `4 小时`, `30 分钟`, `1.5 小时`.
 *
 * `news.oi.window_ms` is a duration, and its bounds admit anything from five minutes up, so rounding it to
 * whole hours would print `1 小时` beside a live 30-minute threshold on both the gate row and the occupancy
 * card. Returns `""` when there is no policy yet, so a caller can drop the label rather than print a zero.
 */
export function oiWindowLabel(windowMs: number | null | undefined): string {
  if (!windowMs || !Number.isFinite(windowMs) || windowMs <= 0) return "";
  const minutes = windowMs / 60_000;
  if (minutes < 60) return `${trimZeros(minutes)} 分钟`;
  return `${trimZeros(minutes / 60)} 小时`;
}

function trimZeros(value: number): string {
  return value.toFixed(2).replace(/\.?0+$/, "");
}

/**
 * Which measured bucket a frame falls in, from `docs/research/oi-agent-design-2026-08-22.md` §1.5 as the
 * trading lane encodes it in configuration (#207).
 *
 * This is an annotation, not a gate: News pushed or withheld this frame on its own thresholds, and whether
 * the capital lane would have opened on it is a separate question with separate numbers. `positive` marks
 * the one whale-profit bucket the research measured with a positive mean; `best` and `worst` mark the two
 * ends of the open-interest size buckets. A frame with no measurement gets nothing rather than a guess.
 *
 * The quadrant and the pre-frame 1 h move — the other two discriminators the research uses — need the price
 * one hour *before* the frame, which the News price plane does not store: `news_event_reactions` is anchored
 * at the Event and keeps p0/p1/p4 only. They are deliberately absent rather than approximated.
 */
export type OiBucketTone = "positive" | "best" | "worst";
export type OiBucket = { label: string; title: string; tone: OiBucketTone };

/** §1.5: >200M is the best-performing open-interest bucket, 10–50M the worst. */
const OI_VALUE_BEST_USD = 200_000_000;
const OI_VALUE_WORST_MAX_USD = 50_000_000;
const OI_VALUE_WORST_MIN_USD = 10_000_000;

export function oiBuckets(
  oi: NewsFeedOi | null | undefined,
  floors: NewsOiTradeFloors,
): OiBucket[] {
  if (!oi?.parsed) return [];
  const buckets: OiBucket[] = [];
  const profit = oi.whale_long_profit_bps;
  // A zero floor is not a floor. It arrives when the console is newer than the API, and `profit >= 0` would
  // stamp "研究里唯一均值为正的分桶" on every frame with a tooltip claiming a 0.00% threshold.
  if (
    floors.min_whale_long_profit_bps > 0 &&
    profit != null &&
    profit >= floors.min_whale_long_profit_bps
  ) {
    buckets.push({
      label: "盈利正桶",
      title: `鲸鱼多头盈利 ≥ ${oiPercent(floors.min_whale_long_profit_bps)}：研究里唯一均值为正的分桶`,
      tone: "positive",
    });
  }
  const value = oi.oi_value_usd;
  if (value != null && value >= OI_VALUE_BEST_USD) {
    buckets.push({
      label: "持仓最优桶",
      title: "持仓 ≥ 2 亿：研究里表现最好的持仓分桶",
      tone: "best",
    });
  } else if (value != null && value >= OI_VALUE_WORST_MIN_USD && value < OI_VALUE_WORST_MAX_USD) {
    buckets.push({
      label: "持仓最差桶",
      title: "持仓 1000 万 – 5000 万：研究里表现最差的持仓分桶",
      tone: "worst",
    });
  }
  return buckets;
}
