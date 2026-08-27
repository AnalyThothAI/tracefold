import type {
  TradingCase,
  TradingCounts,
  TradingGateDecision,
  TradingStatus,
} from "../api/tradingQueries";

import { gateReasonLabel } from "./tradingLabels";
import { policyRuleZh } from "./tradingOiLedger";

/**
 * The page's answer layer: what happened to today's frames, said in words (#273).
 *
 * The console this replaces printed the ledger's own vocabulary — `smart_money_oi_change_below_floor`
 * beside a bar chart whose first two rows counted a rolling 24 h and whose last four counted a UTC day —
 * and an operator reading it could not tell whether the lane was quiet, broken, or working exactly as
 * configured. Everything here exists to answer three questions in order: how many signals arrived, where
 * each one stopped, and what the next order is waiting for.
 *
 * **Nothing here re-decides anything.** A sentence is built from the rule the backend already named plus
 * the number that rule is about; the comparison itself is never re-run in the browser. That is not
 * fussiness — `oi_change` refuses below its floor while the ratio refuses at or below its own, so a
 * screen that re-derived "did this pass" would disagree with the case at exactly the boundary values the
 * strategy tests exist to pin. The threshold is read from the running config for the same reason: a
 * literal here would describe a strategy no case was decided by the moment someone edits one.
 */

const SMART_MONEY_STRATEGY = "oi_smart_money_momentum_v1";

export type StrategyNumbers = {
  minOiChangeBps: number | null;
  minWhaleOiRatioBps: number | null;
  minWhaleLongProfitBps: number | null;
  maxPriceMoveBps: number | null;
  measurementWindowMs: number | null;
};

/** Every value on `status.strategies[].config` is a string; a missing key is `null`, never a zero. */
function numberOrNull(
  config: Readonly<Record<string, string>> | undefined,
  key: string,
): number | null {
  const raw = config?.[key];
  if (raw == null || raw === "") return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function strategyNumbers(status: TradingStatus | undefined): StrategyNumbers {
  return numbersFrom(
    status?.strategies?.find((row) => row.strategy_id === SMART_MONEY_STRATEGY)?.config,
  );
}

/**
 * The thresholds one *case* was decided against, falling back to the running ones.
 *
 * A case froze its own `strategy_config`, and that is the only configuration its `policy_reason` is
 * true under. Explaining it with today's numbers produces sentences that cannot be — a 700 bps frame
 * refused under the old 1000 bps floor rendering as "7.00%，未达 5.00%" — and, worse, names the wrong
 * bottleneck in the headline the moment an operator edits a threshold. The fallback is for cases
 * frozen before the projection carried the field; those are the only ones where today's numbers are
 * the best available guess, and they are all older than the current config anyway.
 */
export function caseNumbers(
  frozen: Readonly<Record<string, string>> | undefined,
  running: StrategyNumbers,
): StrategyNumbers {
  return frozen && Object.keys(frozen).length > 0 ? numbersFrom(frozen) : running;
}

function numbersFrom(config: Readonly<Record<string, string>> | undefined): StrategyNumbers {
  return {
    maxPriceMoveBps: numberOrNull(config, "max_price_move_bps"),
    measurementWindowMs: numberOrNull(config, "measurement_window_ms"),
    minOiChangeBps: numberOrNull(config, "min_oi_change_bps"),
    minWhaleLongProfitBps: numberOrNull(config, "min_whale_long_profit_bps"),
    minWhaleOiRatioBps: numberOrNull(config, "min_whale_oi_ratio_bps"),
  };
}

export function percent(bps: number | null | undefined, digits = 2): string {
  if (bps == null) return "—";
  return `${(bps / 100).toFixed(digits)}%`;
}

/** "5 分钟" from the frozen window, so the sentence never asserts an interval nobody proved. */
export function windowLabel(ms: number | null): string {
  if (ms == null || ms <= 0) return "该窗口";
  const minutes = Math.round(ms / 60_000);
  return minutes >= 1 ? `${minutes} 分钟` : `${Math.round(ms / 1000)} 秒`;
}

export type Refusal = {
  /** The plain sentence. Always populated. */
  sentence: string;
  /** The measured value the rule is about, when the ledger carries one. */
  measured: string | null;
  /** The threshold it was compared against, when the running config publishes one. */
  threshold: string | null;
};

/**
 * One case's refusal, as a sentence an operator can act on.
 *
 * `evidence` is the admission ledger's copy of the frame's own four numbers, which is why this can name
 * a measured value at all: the case row carries the rule, the gate row carries the number, and they
 * share a `case_id`. A rule with no number available still gets a sentence — an unexplained refusal is
 * worse than an unquantified one.
 */
export function refusalOf(
  reason: string | null | undefined,
  {
    evidence,
    numbers,
    preMoveBps,
  }: {
    evidence?: TradingGateDecision["gate_evidence"] | null;
    numbers: StrategyNumbers;
    preMoveBps?: number | null;
  },
): Refusal {
  if (reason == null || reason === "") {
    // A case still `PENDING`/`RUNNING` is inserted with no policy columns at all, and the shared
    // `policyRuleZh` answers a null with the literal 交易地板. Printing that under 离下一单还差什么
    // would state a floor as *the* reason a case has no order, for a case no strategy has looked at
    // yet — an invented refusal, which is worse than an absent one.
    return { measured: null, sentence: "尚未判定", threshold: null };
  }
  const window = windowLabel(numbers.measurementWindowMs);
  const oiChange = evidence?.oi_change_bps ?? null;
  const ratio = evidence?.whale_oi_ratio_bps ?? null;
  const profit = evidence?.whale_long_profit_bps ?? null;
  const oiValue = evidence?.oi_value_usd ?? null;

  switch (reason) {
    case "smart_money_oi_change_below_floor":
      return {
        measured: percent(oiChange),
        sentence: `${window}持仓增幅 ${percent(oiChange)}，未达 ${percent(numbers.minOiChangeBps)} 门槛`,
        threshold: percent(numbers.minOiChangeBps),
      };
    case "smart_money_ratio_below_or_equal_floor":
      return {
        measured: percent(ratio),
        sentence: `大户持仓占比 ${percent(ratio)}，未超过 ${percent(numbers.minWhaleOiRatioBps)}`,
        threshold: percent(numbers.minWhaleOiRatioBps),
      };
    case "smart_money_profit_not_positive":
      return {
        measured: percent(profit),
        sentence: `大户盈利指标 ${percent(profit)}，不为正`,
        threshold: percent(numbers.minWhaleLongProfitBps),
      };
    case "move_above_band_chasing":
      return {
        measured: percent(preMoveBps),
        sentence: `价格已先涨 ${percent(preMoveBps)}，超过 ${percent(numbers.maxPriceMoveBps)} 追价上限`,
        threshold: percent(numbers.maxPriceMoveBps),
      };
    case "price_direction_not_confirmed":
      return {
        measured: percent(preMoveBps),
        sentence:
          preMoveBps == null
            ? "帧前没有可比的收盘价，价格方向无从确认"
            : `价格未同步上涨（帧前 ${percent(preMoveBps)}）`,
        threshold: null,
      };
    case "not_oi_rise":
      return { measured: null, sentence: "这一帧是持仓下降，不是上升", threshold: null };
    case "source_window_mismatch":
      return {
        measured: null,
        sentence: `无法证明这一帧测的是${window}窗口`,
        threshold: null,
      };
    case "oi_context_missing":
      return { measured: null, sentence: "新闻旁没有同标的的持仓数据", threshold: null };
    case "oi_value_below_floor":
      return {
        measured: oiValue == null ? null : `$${(oiValue / 1_000_000).toFixed(1)}M`,
        sentence:
          oiValue == null
            ? "总持仓额低于流动性地板"
            : `总持仓额 $${(oiValue / 1_000_000).toFixed(1)}M，低于流动性地板`,
        threshold: null,
      };
    case "smart_money_momentum_long":
      return { measured: null, sentence: "四项条件全部通过", threshold: null };
    default:
      // A rule this map has not learned yet still gets the shared vocabulary, and failing that, itself.
      return { measured: null, sentence: policyRuleZh(reason), threshold: null };
  }
}

export type FunnelLevel = {
  label: string;
  value: number;
  /** The one thing most of this level's losses stopped on, already in words. */
  note: string | null;
};

/**
 * The five levels a frame passes through, all on the ledger's rolling 24 h.
 *
 * The old funnel drew seven bars on three different clocks — two rolling, four on the UTC budget day,
 * one unbounded — and labelled the seam instead of removing it. Exposure is not a funnel level for that
 * reason: an open position from yesterday is real and current, and a bar chart is the wrong place to
 * say so. It has its own card.
 */
export type LaneCounts = {
  seen: number;
  cased: number;
  allowed: number;
  submitted: number;
  closed: number;
};

/**
 * The five numbers this page is about, derived once so two panels cannot disagree.
 *
 * Only the first comes from the gate ledger; everything below it is counted off the case and order
 * tables. The two sources describe the same population but are keyed on different clocks — a gate row
 * on when the *frame* was observed, a case on when it was *created* — and mixing them drew 策略放行 as
 * a larger number than the 建成案例 above it, a funnel disproving itself on screen.
 *
 * What that buys is that the *same quantity* is never counted two ways. What it does not buy is a
 * guaranteed subset relation, and this is deliberately not claimed anywhere the reader can see: the
 * three ledgers are each bounded on their own timestamp, so an order created just inside the window
 * from a case created just outside it lands in `submitted` without its case in `allowed`. At a
 * 24-hour boundary a lower level can therefore read higher than the one above it. Clamping would be
 * the console inventing a number to protect a shape; the counts stay exact.
 *
 * A second seam closes itself: `seen` counts the OI lane, because the gate ledger's read model is OI
 * by construction, while the case levels count every trigger kind. Since #273 a News trigger no
 * longer freezes a case at all, so the two populations are the same one — but for the first window
 * after that deploy, cases created under the old behaviour are still inside 24 h and `建成案例`
 * reads high by however many of them there were.
 */
export function laneCounts(counts: TradingCounts): LaneCounts {
  const admission = counts.candidate_counts_24h ?? {};
  return {
    allowed: counts.policy_allowed_24h ?? 0,
    cased: Object.values(counts.cases_by_state ?? {}).reduce((sum, value) => sum + value, 0),
    closed: counts.orders_by_state?.CLOSED ?? 0,
    seen: Object.values(admission).reduce((sum, value) => sum + value, 0),
    submitted: Object.values(counts.orders_by_state ?? {}).reduce((sum, value) => sum + value, 0),
  };
}

export function funnelLevels(counts: TradingCounts): FunnelLevel[] {
  const { allowed, cased, closed, seen, submitted } = laneCounts(counts);

  return [
    { label: "上游帧到达", note: null, value: seen },
    {
      label: "建成案例",
      note: seen > cased ? topAdmissionRefusal(counts) : null,
      value: cased,
    },
    // No note on the strategy level, deliberately: the headline above already names that rule with
    // its measurement, and a page that prints the same sentence twice teaches a reader to skip both.
    { label: "策略放行", note: null, value: allowed },
    { label: "提交订单", note: null, value: submitted },
    { label: "已了结", note: null, value: closed },
  ];
}

/** The admission reason that refused the most frames, in words. `freeze:case_created` is not one. */
export function topAdmissionRefusal(counts: TradingCounts): string | null {
  const entries = Object.entries(counts.candidate_reasons_24h ?? {})
    .filter(([key]) => key !== "freeze:case_created")
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
  const top = entries[0];
  return top ? `多数停在：${gateReasonLabel(top[0])}（${top[1]}）` : null;
}

/**
 * The rule most of the day's cases stopped on, and how many.
 *
 * Counted over the cases the console actually lists rather than over a server-side grouping, so the
 * sentence at the top of the page and the table at the bottom cannot disagree about the same population.
 */
export function bindingCaseRule(
  cases: readonly TradingCase[],
): { reason: string; count: number } | null {
  const tally = new Map<string, number>();
  for (const row of cases) {
    const reason = row.policy_reason;
    if (!reason || row.policy_decision !== "no_trade") continue;
    tally.set(reason, (tally.get(reason) ?? 0) + 1);
  }
  const ranked = [...tally.entries()].sort(
    (left, right) => right[1] - left[1] || left[0].localeCompare(right[0]),
  );
  const top = ranked[0];
  return top ? { count: top[1], reason: top[0] } : null;
}

/** Gate rows carry the frame's numbers; cases carry the rule. `case_id` is what joins them. */
export function evidenceByCase(
  decisions: readonly TradingGateDecision[],
): Map<string, TradingGateDecision> {
  const byCase = new Map<string, TradingGateDecision>();
  for (const decision of decisions) {
    if (decision.case_id) byCase.set(decision.case_id, decision);
  }
  return byCase;
}
