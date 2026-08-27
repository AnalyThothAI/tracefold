import { newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Card } from "@shared/ui/Card";
import { Link } from "react-router-dom";

import type {
  TradingCase,
  TradingCounts,
  TradingGateDecision,
  TradingStatus,
} from "../api/tradingQueries";
import { CASE_STATE_ZH } from "../model/tradingLabels";
import {
  bindingCaseRule,
  evidenceByCase,
  funnelLevels,
  laneCounts,
  refusalOf,
  strategyNumbers,
} from "../model/tradingReadout";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

import "./tradingReadout.css";

/**
 * The sentence a reader needs before any panel below it means anything (#273).
 *
 * A lane at zero orders looks identical to a broken one, and every number on this page was a count of
 * something that did not happen. So the page now opens by saying which of the two it is: how many frames
 * arrived, how far they got, and — when nothing was submitted — the one rule most of them stopped on,
 * with the measurement and the threshold it missed.
 *
 * It draws only from the durable ledger, on one clock. `funnel_today` and the scan counters are
 * deliberately not read here: they reset at UTC midnight and count re-reads of the same frame, so a
 * headline built on them says "18,460 frames" about roughly ninety.
 */
export function TradingHeadline({
  cases,
  counts,
  decisions,
  status,
}: {
  cases: readonly TradingCase[];
  counts: TradingCounts;
  decisions: readonly TradingGateDecision[];
  status: TradingStatus;
}) {
  // The same five numbers the ladder below draws, from the same derivation (#273).
  const { allowed, cased, seen, submitted } = laneCounts(counts);

  const numbers = strategyNumbers(status);
  const evidence = evidenceByCase(decisions);
  const binding = bindingCaseRule(cases);
  const example = binding
    ? cases.find((row) => row.policy_reason === binding.reason)
    : undefined;
  const refusal = binding
    ? refusalOf(binding.reason, {
        evidence: example ? evidence.get(example.case_id)?.gate_evidence : null,
        numbers,
        preMoveBps: example?.pre_move_bps,
      })
    : null;

  const lead =
    seen === 0
      ? "过去 24 小时没有任何上游帧到达。"
      : `过去 24 小时，${seen} 帧到达 · ${cased} 帧建成案例 · ${allowed} 个案例被策略放行 · ${submitted} 笔订单提交。`;

  return (
    <section aria-label="资本通道状态" className="trading-readout">
      <p className="trading-readout-lead">{lead}</p>
      {submitted === 0 && refusal ? (
        <p className="trading-readout-why">
          <b>还没有订单，是因为</b>
          {refusal.sentence}
          <small>
            过去 24 小时 {binding?.count} 个案例停在同一条规则
            {refusal.threshold ? ` · 门槛 ${refusal.threshold}` : ""}
          </small>
        </p>
      ) : null}
      {submitted === 0 && !refusal && seen > 0 ? (
        <p className="trading-readout-why">
          <b>还没有订单。</b>过去 24 小时没有案例走到策略判定，原因见下方漏斗。
        </p>
      ) : null}
      <TradingSourceLine
        note="全部读自准入台账与案例账本，同一个 24 小时窗口"
        path="GET /api/trading/status · GET /api/trading/gate · GET /api/trading/orders"
      />
    </section>
  );
}

/**
 * Where the day's frames stopped — one population, one clock, and the reason in words.
 *
 * The bar chart this replaces mixed a rolling window with a UTC budget day and an unbounded exposure
 * count, and said so in a hint line rather than fixing it. Each level here is a strict subset of the one
 * above it, every count comes from the durable ledger's own 24 h, and the note beside a level names what
 * most of the drop was — which is the only thing a shrinking funnel is actually asking the reader.
 */
export function TradingLadder({ counts }: { counts: TradingCounts }) {
  const levels = funnelLevels(counts);
  const max = Math.max(1, ...levels.map((level) => level.value));

  return (
    <Card flush hint="过去 24 小时 · 帧按观测时刻，案例与订单按建立时刻" title="信号去了哪里">
      <div className="trading-ladder">
        {levels.map((level) => (
          <div className="trading-ladder-row" key={level.label}>
            <small>{level.label}</small>
            <span className="trading-ladder-track">
              {/*
               * A floor of 3px on any non-zero level. Against a first level in the hundreds the ones
               * below it round to a bar too thin to see, and "3 orders" and "no orders" then look
               * identical — which is the one distinction this whole page exists to draw. Zero stays
               * genuinely empty, and the count beside it is always the exact number.
               */}
              <span
                style={
                  level.value === 0
                    ? { width: 0 }
                    : { width: `max(3px, ${Math.round((level.value / max) * 100)}%)` }
                }
              />
            </span>
            <b>{level.value}</b>
            {level.note ? <em>{level.note}</em> : null}
          </div>
        ))}
      </div>
    </Card>
  );
}

/**
 * The most recent decisions, each stating its own measurement against its own threshold.
 *
 * This is the panel that answers "what is the next order waiting for". The rule and the number come
 * from two different durable rows joined on `case_id` — the case knows which rule it stopped on, the
 * admission ledger kept the frame's four numbers — and neither is recomputed here. The raw rule key
 * stays on screen beside the sentence: it is what an operator greps, and a screen that only ever shows
 * a translation quietly becomes a second, unversioned vocabulary.
 */
export function TradingDecisions({
  cases,
  decisions,
  status,
}: {
  cases: readonly TradingCase[];
  decisions: readonly TradingGateDecision[];
  status: TradingStatus;
}) {
  const referrer = useRouteReferrer();
  const numbers = strategyNumbers(status);
  const evidence = evidenceByCase(decisions);
  const rows = cases.slice(0, 12);

  return (
    <Card
      flush
      hint="最近的案例判定 · 实测值对当前门槛"
      title={`离下一单还差什么 · ${cases.length}`}
    >
      <div className="trading-decisions">
        {rows.length === 0 ? (
          <TradingEmptyNote>
            过去 24 小时没有停在判定之前的案例——要么没有帧通过准入，要么通过的都下单了。
          </TradingEmptyNote>
        ) : (
          rows.map((row) => {
            const refusal = refusalOf(row.policy_reason, {
              evidence: evidence.get(row.case_id)?.gate_evidence,
              numbers,
              preMoveBps: row.pre_move_bps,
            });
            return (
              <article className="trading-decision-row" key={row.case_id}>
                <span className="trading-decision-symbol">
                  <Link state={referrer} to={newsSymbolPath(row.base_symbol)}>
                    {row.base_symbol}
                  </Link>
                  <small>{new Date(row.created_at_ms).toISOString().slice(11, 16)}</small>
                </span>
                <span className="trading-decision-why">
                  {refusal.sentence}
                  <code>{row.policy_reason ?? row.policy_decision ?? "—"}</code>
                </span>
                <span className="trading-decision-state" data-state={row.state}>
                  {CASE_STATE_ZH[row.state] ?? row.state}
                </span>
              </article>
            );
          })
        )}
        {cases.length > rows.length ? (
          <p className="trading-decisions-more">
            另有 {cases.length - rows.length} 个案例未列出（本页只显示最近 {rows.length} 个）。
          </p>
        ) : null}
      </div>
    </Card>
  );
}
