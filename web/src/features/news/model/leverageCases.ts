import {
  CASE_STATE_ZH,
  REGIME_ZH,
  STRATEGY_ZH,
  isActiveOrder,
  policyRuleZh,
  strategyCaseLabel,
  type TradingOiLedgerEntry,
} from "@features/trading";

import type { NewsFeedEvent, NewsFeedOi, NewsOiTradeFloors } from "../api/newsQueries";

import { clockTime, relativeTime } from "./newsLabels";
import { oiPercent, oiValueZh } from "./oiSignals";

/**
 * 杠杆异动 — the capital lane's reading of the deterministic OI frames (#256).
 *
 * The console had two ways of looking at that lane and only one surface. `/news/oi` audits *frames*: did the
 * provider line parse, did it clear the push gates, did it take a window slot. This model answers the other
 * question — for the frames that became a capital case, what was decided, on which named rule, and what
 * happened to the money — and it is deliberately a different page because the two use different thresholds
 * and are read at different times.
 *
 * Everything here is a projection of two already-served reads: the deterministic frames from
 * `/api/news/feed` and the capital ledger from `/api/trading/orders`, joined only on the `event_id` the
 * ledger itself published. Nothing is inferred from a symbol and a timestamp — the join the OI table refuses
 * to guess at is not guessed at here either.
 */

export type LeveragePhase = "forming" | "active" | "resolved" | "no_trade";
export type LeverageDecision = "long" | "short" | "no_trade" | "pending";
export type LeverageEvidenceStatus = "support" | "conflict" | "missing" | "na";

export type LeverageEvidenceRow = {
  key: string;
  label: string;
  note: string;
  status: LeverageEvidenceStatus;
};

export type LeveragePlaneItem = { key: string; label: string; note?: string; value: string };

/** How a lane names its own trigger. The card and the detail read this same map. */
export const TRIGGER_LABEL: Record<string, string> = {
  liquidation: "清算帧",
  news: "新闻",
  oi: "OI 帧",
};

export function triggerLabel(triggerKind: string): string {
  return TRIGGER_LABEL[triggerKind] ?? triggerKind;
}

export type LeverageTimelineStep = {
  at: string;
  key: string;
  label: string;
  note: string;
  tone: "step" | "decision" | "capital" | "caution";
};

export type LeverageCase = {
  /** `case_id` — the ledger's own identity, present on every case. It is the row's URL state. */
  id: string;
  age: string;
  base: string;
  caseId: string;
  caseState: string;
  decision: LeverageDecision;
  decidedAtMs: number | null;
  entry: TradingOiLedgerEntry;
  /** The frame, when the loaded frame page holds it. `undefined` is a page boundary, not a missing case. */
  event: NewsFeedEvent | undefined;
  /**
   * The Event id the ledger published, independent of whether the frame is loaded. Non-null only for the
   * deterministic OI trigger; it is not the row's identity, only a legacy link target and a frame key.
   */
  eventId: string | null;
  evidence: LeverageEvidenceRow[];
  /** The capital state as the ledger's own word, or `null` when the frame never authored an intent. */
  capital: string | null;
  createdAtMs: number;
  numbers: string;
  observedAtMs: number;
  phase: LeveragePhase;
  regime: string;
  /** The rule the decision stopped on, in the ledger's own vocabulary. */
  rule: string;
  /** The immutable strategy identity, and the compact label the console prints for it. */
  strategyId: string;
  strategyLabel: string;
  /** `oi` | `news` | `liquidation` — which lane fired. Only `oi` ever has a telemetry frame. */
  triggerKind: string;
  /** `crypto:WIF` — the ledger's own subject key. Not a venue: this lane publishes none. */
  underlyingKey: string;
  why: string;
};

/** The handful of ledger fields both halves of `TradingOiLedgerEntry` can answer, read once. */
type CaseFacts = {
  caseId: string;
  caseState: string;
  createdAtMs: number;
  decidedAtMs: number | null;
  observedAtMs: number;
  policyDecision: string | null;
  policyReason: string | null;
  regime: string | null;
  strategyId: string;
  triggerKind: string;
};

function caseFacts(entry: TradingOiLedgerEntry): CaseFacts {
  if (entry.kind === "order") {
    const order = entry.value;
    return {
      caseId: order.case_id,
      caseState: order.case_state,
      createdAtMs: order.created_at_ms,
      // An order row carries no `decided_at_ms`: the decision that authored it is the case's, and the
      // order's own clock starts later. Naming it here would date the decision to the write that followed.
      decidedAtMs: null,
      observedAtMs: order.case_observed_at_ms ?? order.created_at_ms,
      policyDecision: order.policy_decision ?? null,
      policyReason: order.policy_reason ?? null,
      regime: order.regime ?? null,
      strategyId: order.strategy_id,
      triggerKind: order.trigger_kind,
    };
  }
  const record = entry.value;
  return {
    caseId: record.case_id,
    caseState: record.state,
    createdAtMs: record.created_at_ms,
    decidedAtMs: record.decided_at_ms ?? null,
    observedAtMs: record.observed_at_ms,
    policyDecision: record.policy_decision ?? null,
    policyReason: record.policy_reason ?? null,
    regime: record.regime ?? null,
    strategyId: record.strategy_id,
    triggerKind: record.trigger_kind,
  };
}

const DECISION_BY_POLICY: Record<string, LeverageDecision> = {
  long: "long",
  no_trade: "no_trade",
  short: "short",
};

/**
 * One case per ledger entry, with its frame attached when the loaded frame page happens to hold it.
 *
 * The iteration is over the *ledger*, not over the frames, and it is keyed by `case_id`. Both halves of
 * that matter and the second one was wrong first: keying by `event_id` looks natural on a page about OI
 * frames, but the server publishes an `event_id` only for the deterministic OI trigger — a news- or
 * liquidation-triggered case carries `null` by design, because its source key is a content hash no Event id
 * rebuilds. Indexing by it dropped every case the lane actually produced, and the page told the operator
 * 「24 小时内没有成案」 while nine sat in the ledger. `case_id` is the ledger's own identity and always there.
 *
 * The frame is attached when the loaded frame page happens to hold it, and is decoration either way: it
 * carries the wire line and the OI measurements, not the case's identity.
 *
 * A frame with no ledger entry is still not a case and is still not listed: the OI audit owns "this frame
 * existed and nothing opened on it" in full.
 */
export function leverageCases(
  frames: readonly NewsFeedEvent[],
  ledger: Map<string, TradingOiLedgerEntry>,
  floors: NewsOiTradeFloors,
  nowMs: number,
): LeverageCase[] {
  const byEventId = new Map(frames.map((frame) => [frame.event_id, frame]));
  return [...ledger.values()]
    .map((entry) => buildCase(byEventId.get(entry.value.event_id ?? ""), entry, floors, nowMs))
    .sort(leverageOrder);
}

function buildCase(
  event: NewsFeedEvent | undefined,
  entry: TradingOiLedgerEntry,
  floors: NewsOiTradeFloors,
  nowMs: number,
): LeverageCase {
  const facts = caseFacts(entry);
  const decision = DECISION_BY_POLICY[facts.policyDecision ?? ""] ?? "pending";
  return {
    age: relativeTime(facts.observedAtMs),
    base: entry.value.base_symbol || event?.oi?.symbol || "—",
    capital: entry.kind === "order" ? entry.value.state : null,
    caseId: facts.caseId,
    caseState: facts.caseState,
    createdAtMs: facts.createdAtMs,
    decidedAtMs: facts.decidedAtMs,
    decision,
    entry,
    event,
    eventId: entry.value.event_id ?? null,
    evidence: evidenceRows(event?.oi, facts.strategyId, facts.triggerKind, floors),
    id: facts.caseId,
    numbers: frameNumbers(event?.oi, facts.triggerKind),
    observedAtMs: facts.observedAtMs,
    phase: phaseOf(entry, decision),
    regime: facts.regime ? (REGIME_ZH[facts.regime] ?? facts.regime) : "象限未定",
    rule: facts.policyReason ?? "—",
    strategyId: facts.strategyId,
    strategyLabel: strategyCaseLabel(facts.strategyId),
    triggerKind: facts.triggerKind,
    underlyingKey: entry.value.underlying_key,
    why: whySentence(entry, facts, decision, nowMs),
  };
}

/**
 * Which of the four phases a case is in.
 *
 * Read from the ledger's own states, never from elapsed time: a case is `active` because an order is in a
 * state that holds — or may yet turn out to hold — exposure, and `resolved` because the ledger closed it.
 * A clock cannot see either.
 *
 * `forming` is the narrow case and has to stay narrow. `BLOCKED` is terminal even though its
 * `policy_decision` is a direction: the strategy said long and `_place` refused — daily order cap, one
 * position per underlying, a blacklist re-read, a sizing rejection — so the case never authored an intent
 * and never will. Reading its direction as "still forming" put a dead case under 正在发生 with a red LONG
 * chip and a heading promising a live setup.
 */
const TERMINAL_CASE_STATES = new Set(["BLOCKED", "NO_TRADE", "POLICY_REJECTED"]);

function phaseOf(entry: TradingOiLedgerEntry, decision: LeverageDecision): LeveragePhase {
  if (entry.kind === "order") {
    return isActiveOrder(entry.value) ? "active" : "resolved";
  }
  if (decision === "no_trade" || TERMINAL_CASE_STATES.has(entry.value.state)) return "no_trade";
  return "forming";
}

/**
 * The frame's three published measurements on one line, in the units the reader card uses.
 *
 * `undefined` and `parsed: false` are different facts and say so: one is a frame this page did not load,
 * the other is a frame the provider's line broke on. Collapsing them would blame the parser for a page
 * boundary.
 */
function frameNumbers(oi: NewsFeedOi | null | undefined, triggerKind: string): string {
  // Three different absences, three different sentences. A news-triggered case never had a telemetry frame
  // at all; blaming a page boundary or a parser for that would invent a fault that does not exist.
  if (oi === undefined) return triggerKind === "oi" ? "原帧不在本页帧里" : "非 OI 触发 · 无遥测帧";
  if (!oi?.parsed) return "帧未解析";
  const change =
    oi.oi_change_bps == null
      ? "—"
      : `${oi.oi_change_bps < 0 ? "−" : "+"}${oiPercent(Math.abs(oi.oi_change_bps))}`;
  return `OI ${change} · 持仓 ${oiValueZh(oi.oi_value_usd)} · 鲸盈 ${oiPercent(oi.whale_long_profit_bps)}`;
}

/**
 * One sentence naming what happened, from the ledger's own words.
 *
 * There is no thesis to quote for most cases: `oi_momentum_v1` is a pure rule and produces no narrative at
 * all, and `program_output.decision.thesis_zh` exists only for the model lane. Rather than paraphrase a
 * decision nobody wrote, this names the rule and lets `policyRuleZh` translate it — the same vocabulary the
 * OI table's 交易判定 column already uses, so the two surfaces cannot describe one case differently.
 */
function whySentence(
  entry: TradingOiLedgerEntry,
  facts: CaseFacts,
  decision: LeverageDecision,
  nowMs: number,
): string {
  if (decision === "pending") {
    return nowMs - facts.observedAtMs > STALE_PENDING_MS
      ? "判定未落库：策略已认领这一帧，结论迟迟没有写回。"
      : "判定进行中：策略已认领这一帧，结论尚未写回。";
  }
  if (decision === "no_trade") return `不交易：${policyRuleZh(facts.policyReason)}。`;
  const state = CASE_STATE_ZH[facts.caseState] ?? facts.caseState;
  if (entry.kind === "order") {
    // A `policy_reason` on an allowed case names the rule it *passed*; most allowed cases carry none, and
    // "交易地板——已下单" would read as the floors having stopped something. Say the step instead.
    return facts.policyReason
      ? `${policyRuleZh(facts.policyReason)}——${state}，资本闭环见下。`
      : `策略放行 · ${state}，资本闭环见下。`;
  }
  return facts.policyReason ? `${policyRuleZh(facts.policyReason)}。` : `${state}。`;
}

/** Past this, "still deciding" stops being an explanation: the lane's own turn budget is far shorter. */
const STALE_PENDING_MS = 5 * 60_000;

/**
 * The evidence matrix, with exactly four states and no fifth.
 *
 * `missing` is a first-class answer and the most common one here. The pre-frame price move, funding and
 * liquidity are inputs the capital lane consumes but does not publish, and the News price plane stores only
 * the Event-anchored p0/p1/p4 — there is no hour-before price to read. Writing 缺失 is the whole point: a
 * matrix that silently omitted the rows it cannot fill would read as "everything checked out".
 */
export function evidenceRows(
  oi: NewsFeedOi | null | undefined,
  strategyId: string,
  triggerKind: string,
  floors: NewsOiTradeFloors,
): LeverageEvidenceRow[] {
  const parsed = Boolean(oi?.parsed);
  const change = oi?.oi_change_bps ?? null;
  const value = oi?.oi_value_usd ?? null;
  const profit = oi?.whale_long_profit_bps ?? null;
  const ratio = oi?.whale_oi_ratio_bps ?? null;
  return [
    {
      key: "oi",
      label: "OI",
      note:
        oi === undefined
          ? triggerKind === "oi"
            ? "原帧不在本页帧里（帧按页取）"
            : "非 OI 触发的案例，本来就没有遥测帧"
          : !parsed
            ? "帧未解析"
            : change == null
              ? "本帧未记录 OI 变动"
              : `${change < 0 ? "减仓" : "增仓"} ${frameChange(change)} · 持仓 ${oiValueZh(value)}`,
      // A parsed frame may still carry no `oi_change_bps`. Calling that a conflict asserts a direction the
      // frame does not have; it is the same absence every other row spells 缺失.
      status: !parsed || change == null ? "missing" : change > 0 ? "support" : "conflict",
    },
    {
      key: "value",
      label: "规模",
      note: floorNote(value, floors.min_oi_value_usd, (amount) => oiValueZh(amount)),
      status: floorStatus(value, floors.min_oi_value_usd),
    },
    {
      key: "whale",
      label: "鲸鱼",
      note: `${floorNote(profit, floors.min_whale_long_profit_bps, (amount) => oiPercent(amount))}${
        ratio == null ? "" : ` · 占比 ${oiPercent(ratio)}`
      }`,
      status: floorStatus(profit, floors.min_whale_long_profit_bps),
    },
    {
      key: "price",
      label: "价格",
      note: "帧前 1h 已走行情未发布 · 显式缺口",
      status: "missing",
    },
    {
      /*
       * `缺失`, not `支持`. The strategy *requiring* alignment is not the strategy having *found* it —
       * `news_oi_alignment_v1` rejects with `news_context_missing`, `model_contradicts_regime` and four
       * other alignment failures, and none of them reaches this browser. Stamping 支持 on the strategy id
       * put "the news supports this" on a case rejected *because* the news contradicted the regime.
       */
      key: "news",
      label: "News",
      note:
        strategyId === "news_oi_alignment_v1"
          ? "本策略要求同向新闻确认；对齐结论未发布 · 显式缺口"
          : "oi_only 案例不要求新闻确认",
      status: strategyId === "news_oi_alignment_v1" ? "missing" : "na",
    },
    { key: "funding", label: "Funding", note: "未获得 · 显式缺口", status: "missing" },
    { key: "liquidity", label: "流动性", note: "spread / depth 未验证", status: "missing" },
  ];
}

function frameChange(bps: number | null): string {
  if (bps == null) return "—";
  return `${bps < 0 ? "−" : "+"}${oiPercent(Math.abs(bps))}`;
}

/**
 * The measurement against the floor **as configured today**.
 *
 * Said in the note rather than left implicit: the case was judged against the config frozen into its own
 * manifest, which this surface does not publish, so an operator who has since retuned
 * `trading.policy.min_whale_long_profit_bps` can see 鲸鱼 支持 beside 不交易：鲸盈利未达地板. The rule under
 * the matrix is what the case actually stopped on; this row is the comparison a reader can make now.
 */
function floorNote(
  measured: number | null,
  floor: number,
  format: (amount: number) => string,
): string {
  if (measured == null) return "未获得";
  if (floor <= 0) return `${format(measured)} · 未配置地板`;
  return `${format(measured)} ${measured >= floor ? "≥" : "<"} 现行地板 ${format(floor)}`;
}

function floorStatus(measured: number | null, floor: number): LeverageEvidenceStatus {
  if (measured == null) return "missing";
  // A zero floor is not a floor; it arrives when the console is newer than the API, and calling every
  // measurement "支持" against a threshold nobody configured would invent a pass.
  if (floor <= 0) return "na";
  return measured >= floor ? "support" : "conflict";
}

/** The three planes, each frozen at its own moment and never mixed with the others. */
export function leveragePlanes(
  item: LeverageCase,
  quotePrice: string | null,
  nowMs: number,
): Record<"t0" | "now" | "out", LeveragePlaneItem[]> {
  const order = item.entry.kind === "order" ? item.entry.value : null;
  const oi = item.event?.oi;
  return {
    t0: [
      { key: "cutoff", label: "CUTOFF", note: "事实截断点", value: clockTime(item.observedAtMs) },
      {
        key: "entry",
        label: "冻结入场参考",
        note: order ? "不随现价变" : "未成单",
        value: order?.entry_reference ?? "—",
      },
      {
        key: "stop",
        label: "冻结止损",
        note: order ? "随单提交" : "—",
        value: order?.stop_price ?? "—",
      },
      {
        key: "oi",
        label: "OI 事实",
        note:
          oi == null
            ? item.triggerKind === "oi"
              ? "帧按页取"
              : "本通道无帧"
            : oi.parsed
              ? "帧内测量"
              : "帧未解析",
        value: item.numbers,
      },
      {
        key: "strategy",
        label: "策略 · 规则",
        note: STRATEGY_ZH[item.strategyId] ?? "",
        value: `${item.strategyLabel} · ${item.rule}`,
      },
    ],
    now: [
      { key: "quote", label: "当前报价", note: "独立轮询 · 不回写案例", value: quotePrice ?? "—" },
      { key: "age", label: "案例年龄", note: "", value: item.age },
      { key: "state", label: "订单状态", note: order ? "" : "未成单", value: order?.state ?? "—" },
      {
        key: "hold",
        label: "持有窗口",
        note: order?.position_opened_at_ms == null ? "从首笔成交起算" : "",
        value: holdWindow(
          order?.must_close_at_ms ?? null,
          order?.position_opened_at_ms ?? null,
          nowMs,
        ),
      },
    ],
    out: [
      { key: "exit", label: "出场价", note: "", value: order?.exit_price ?? "—" },
      { key: "reason", label: "了结方式", note: "", value: order?.exit_reason ?? "—" },
      {
        key: "realized",
        label: "已实现",
        note: order?.realized_bps == null ? "出场未测量" : "",
        value: order?.realized_bps == null ? "—" : signedBps(order.realized_bps),
      },
      {
        key: "closed",
        label: "平仓时间",
        note: "",
        value: order?.position_closed_at_ms == null ? "—" : clockTime(order.position_closed_at_ms),
      },
    ],
  };
}

function holdWindow(
  mustCloseAtMs: number | null,
  openedAtMs: number | null,
  nowMs: number,
): string {
  if (mustCloseAtMs == null || openedAtMs == null) return "未起算";
  const left = mustCloseAtMs - nowMs;
  if (left <= 0) return "已到期";
  const minutes = Math.floor(left / 60_000);
  return `剩 ${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function signedBps(value: number): string {
  return `${value < 0 ? "−" : value > 0 ? "+" : ""}${Math.abs(value)}bps`;
}

/** TRIGGER → STRATEGY → CASE → ORDER, from the timestamps the ledger actually recorded. */
export function leverageTimeline(item: LeverageCase): LeverageTimelineStep[] {
  const steps: Array<LeverageTimelineStep & { atMs: number }> = [
    {
      atMs: item.event?.opened_at_ms ?? item.observedAtMs,
      at: clockTime(item.event?.opened_at_ms ?? item.observedAtMs),
      key: "trigger",
      label: "Trigger",
      note:
        item.triggerKind === "oi"
          ? "OI 帧落库 · telemetry_deterministic"
          : `${triggerLabel(item.triggerKind)}落库 · 触发资本通道`,
      tone: "step",
    },
    {
      atMs: item.createdAtMs,
      at: clockTime(item.createdAtMs),
      key: "case",
      label: "Case",
      note: `${item.caseId} · ${CASE_STATE_ZH[item.caseState] ?? item.caseState}`,
      tone: "step",
    },
  ];
  if (item.decidedAtMs != null) {
    steps.push({
      atMs: item.decidedAtMs,
      at: clockTime(item.decidedAtMs),
      key: "strategy",
      label: "Strategy",
      note: `${item.strategyLabel} → ${item.decision} · ${policyRuleZh(item.rule)}`,
      tone: "decision",
    });
  }
  if (item.entry.kind === "order") {
    const order = item.entry.value;
    steps.push({
      atMs: order.updated_at_ms,
      at: clockTime(order.updated_at_ms),
      key: "order",
      label: "Order",
      note: `${order.state}${order.state_reason ? ` · ${order.state_reason}` : ""}`,
      tone: order.state === "AMBIGUOUS" || order.state === "UNPROTECTED" ? "caution" : "capital",
    });
  }
  /*
   * Sorted by the clock the ledger wrote, not by the order the stages are named in. `settle_case` stamps
   * `decided_at_ms` *after* the case row exists, so printing STRATEGY before CASE ran the visible clock
   * backwards between two adjacent rows — under a heading promising the ledger's own timestamps.
   */
  return steps.sort((a, b) => a.atMs - b.atMs).map(({ atMs: _atMs, ...step }) => step);
}

export type LeverageTab = "live" | "directional" | "no_trade" | "done";

/**
 * The four questions a reader arrives with, in the order the artifact asks them.
 *
 * `live` is the default because it is the only tab whose contents can still change: a case that is forming
 * or holding exposure is the one a reader can still act on knowing about.
 */
export const LEVERAGE_TABS: Record<
  LeverageTab,
  { label: string; predicate: (item: LeverageCase) => boolean }
> = {
  live: {
    label: "正在发生",
    predicate: (item) => item.phase === "active" || item.phase === "forming",
  },
  directional: {
    label: "有方向",
    predicate: (item) =>
      (item.decision === "long" || item.decision === "short") && item.phase === "active",
  },
  no_trade: { label: "不交易", predicate: (item) => item.phase === "no_trade" },
  done: { label: "已结束", predicate: (item) => item.phase === "resolved" },
};

const LEVERAGE_TAB_KEYS = Object.keys(LEVERAGE_TABS) as LeverageTab[];

/**
 * `in` was wrong here and the bug was not cosmetic: it walks the prototype chain, so `?lev=toString`
 * satisfied the guard, `LEVERAGE_TABS.toString.predicate` was `undefined`, and the route threw as soon as
 * the lane had a single case. Match the declared keys, the way `parseOiTab` next door matches its array.
 */
export function parseLeverageTab(value: string | null): LeverageTab {
  return value != null && LEVERAGE_TAB_KEYS.includes(value as LeverageTab)
    ? (value as LeverageTab)
    : "live";
}

export function leverageTabCount(cases: readonly LeverageCase[], tab: LeverageTab): number {
  return cases.filter((item) => LEVERAGE_TABS[tab].predicate(item)).length;
}

/**
 * Explainable order, and no composite score.
 *
 * Capital at risk first, then a formed direction, then recency. Every step is a fact a reader can check
 * against the row it moved; a weighted score would be unarguable, which on a page about money is worse than
 * being crude.
 */
const RISK_STATES = new Set([
  "AMBIGUOUS",
  "UNPROTECTED",
  "MANUAL_REVIEW_REQUIRED",
  "SAFETY_CLOSING",
]);

export function leverageOrder(a: LeverageCase, b: LeverageCase): number {
  const risk = rank(b) - rank(a);
  return risk !== 0 ? risk : b.observedAtMs - a.observedAtMs;
}

function rank(item: LeverageCase): number {
  if (item.capital != null && RISK_STATES.has(item.capital)) return 3;
  if (item.phase === "active") return 2;
  if (item.decision === "long" || item.decision === "short") return 1;
  return 0;
}

/**
 * How many listed cases have no frame on the loaded page.
 *
 * Every case in the ledger batch is listed either way — the frame is decoration on a case, not its
 * identity. But those rows carry no wire line and no OI measurements, and a page that showed them without
 * saying why would look like a lane that stopped measuring. Counted exactly, said out loud
 * (`docs/FRONTEND.md`: no silent caps).
 */
export function leverageFramelessCount(cases: readonly LeverageCase[]): number {
  return cases.filter((item) => item.event === undefined).length;
}
