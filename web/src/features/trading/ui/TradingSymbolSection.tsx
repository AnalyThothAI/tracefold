import { Card } from "@shared/ui/Card";

import { useTradingCasesWithToken, useTradingIntentsWithToken } from "../api/tradingQueries";
import { caseStateLabel } from "../model/tradingCases";
import { INTENT_STATE_NOTE, caseClock, policyLabel, policyReasonLabel } from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

import "./trading.css";
import "./tradingSymbolSection.css";

/**
 * One token's capital history: the Cases decided for it, and the Intents those Cases handed over.
 *
 * Two reads, two aggregates, joined on `case_id` for display only. Neither read invents the other's
 * fields: a Case with no Intent says 未形成, which is a fact the Case table holds, not an inference from
 * an absent execution row.
 */
export function TradingSymbolSection({ base, token }: { base: string; token: string }) {
  const casesQuery = useTradingCasesWithToken(token, base);
  const intentsQuery = useTradingIntentsWithToken(token, base);
  const intents = new Map((intentsQuery.data?.intents ?? []).map((item) => [item.case_id, item]));
  const rows = [...(casesQuery.data?.cases ?? [])].sort(
    (a, b) => b.created_at_ms - a.created_at_ms,
  );

  return (
    <Card
      flush
      hint={`过去 ${casesQuery.data?.window_hours ?? 24} 小时的资本案例；旧订单台账不进入此读模型`}
      title="资本复盘 · Case → Intent → Outcome"
    >
      {rows.length ? (
        <div className="trading-table">
          <div aria-hidden className="trading-case-head">
            <span>CUTOFF</span>
            <span>策略</span>
            <span>Case</span>
            <span>规则</span>
            <span>Intent</span>
            <span>Outcome</span>
          </div>
          {rows.map((record) => {
            const intent = intents.get(record.case_id);
            return (
              <article className="trading-case-row" key={record.case_id}>
                <span>{caseClock(record.observed_at_ms)}</span>
                <span>{policyLabel(record.policy_id)}</span>
                <span>{caseStateLabel(record)}</span>
                <span>{policyReasonLabel(record.policy_reason)}</span>
                <span>{intent ? `${intent.intent_id} · ${intent.execution_state}` : "未形成"}</span>
                <span>
                  {intent?.terminal_outcome ??
                    (intent
                      ? (INTENT_STATE_NOTE[intent.execution_state] ?? intent.execution_state)
                      : "—")}
                </span>
              </article>
            );
          })}
        </div>
      ) : (
        <TradingEmptyNote>
          {casesQuery.isError && !casesQuery.data
            ? "资本通道账本本轮不可用；不能据此断言没有案例。"
            : casesQuery.isPending
              ? "正在读取资本通道账本…"
              : "当前窗口没有这个代币的 Case。"}
        </TradingEmptyNote>
      )}
      <TradingSourceLine path="GET /api/trading/cases?underlying={base} → cases[] · GET /api/trading/intents?underlying={base} → intents[]" />
    </Card>
  );
}
