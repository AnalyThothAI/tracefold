import type { LeverageFunnelStep } from "../../model/leverageCases";
import { NewsSourceLine } from "../chrome/NewsSourceLine";

import "./newsLeverageFunnel.css";

/**
 * What the capital lane did with 24 hours of frames, on the page whether or not anything came of it.
 *
 * The tabs above this are the four case populations, and on a normal production day all four are zero:
 * the lane sees about 110 frames and opens one case. A blank list under four zeroes is a true statement
 * that reads as an outage, and it is the reason an operator arriving at this page concluded the console
 * had stopped working. The funnel is the same 24 hours said as a sentence — how many sources the gate
 * saw, how far they got, and which named rule stopped the most of them.
 *
 * Every number is from `trading_candidate_gate_decisions`, which is keyed on when the *frame* was
 * observed and survives the UTC day roll — unlike `funnel_today`, which is overwritten at midnight and
 * could not answer a question about yesterday at all (#264).
 *
 * It renders no chart. Five counts that fall by an order of magnitude between the first and the last
 * are unreadable as bars, and a log axis on a page about money is a graph that has to be explained.
 */
export function NewsLeverageFunnel({
  reasons,
  steps,
  unavailable,
}: {
  reasons: ReadonlyArray<{ key: string; label: string; value: number }>;
  steps: readonly LeverageFunnelStep[];
  unavailable: boolean;
}) {
  return (
    <section aria-label="资本通道 24 小时漏斗" className="news-leverage-funnel">
      <ol className="news-leverage-funnel-steps">
        {steps.map((step) => (
          <li className="news-leverage-funnel-step" key={step.key}>
            <b>{unavailable ? "—" : step.value}</b>
            <span>{step.label}</span>
            <small>{step.note}</small>
          </li>
        ))}
      </ol>
      <div className="news-leverage-funnel-reasons">
        {unavailable ? (
          <small>资本通道状态读取失败，漏斗与拒因暂不可读。</small>
        ) : reasons.length ? (
          <>
            <small>拦在哪：</small>
            {reasons.map((reason) => (
              <span className="news-leverage-funnel-reason" key={reason.key} title={reason.key}>
                {reason.label}
                <b>{reason.value}</b>
              </span>
            ))}
          </>
        ) : (
          <small>过去 24 小时没有落库的准入判定——闸门还没有见过任何来源。</small>
        )}
      </div>
      <NewsSourceLine path="GET /api/trading/status → counts.candidate_counts_24h · candidate_reasons_24h（trading_candidate_gate_decisions）" />
    </section>
  );
}
