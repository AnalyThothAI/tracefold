import { Card } from "@shared/ui/Card";

import { useTradingIntentsWithToken } from "../api/tradingQueries";
import {
  CASE_STATE_ZH,
  INTENT_STATE_NOTE,
  REGIME_ZH,
  caseClock,
  strategyCaseLabel,
} from "../model/tradingLabels";
import { policyRuleZh, tradingLedgerEntries } from "../model/tradingOiLedger";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

import "./trading.css";
import "./tradingSymbolSection.css";

/** Case → Intent → Outcome history for one token. */
export function TradingSymbolSection({ base, token }: { base: string; token: string }) {
  const query = useTradingIntentsWithToken(token, base);
  const rows = [...tradingLedgerEntries(query.data).values()].sort(
    (a, b) => b.value.created_at_ms - a.value.created_at_ms,
  );

  return (
    <Card
      flush
      hint={`过去 ${query.data?.window_hours ?? 24} 小时的资本案例；旧订单台账不进入此读模型`}
      title="交易复盘 · Case → Intent → Outcome"
    >
      {rows.length ? (
        <div className="trading-table">
          <div aria-hidden className="trading-case-head">
            <span>CUTOFF</span>
            <span>策略</span>
            <span>象限</span>
            <span>Case</span>
            <span>规则</span>
            <span>Intent</span>
            <span>Outcome</span>
          </div>
          {rows.map((entry) => {
            const record = entry.value;
            const intent = entry.kind === "intent" ? entry.value : null;
            const observedAt = intent?.case_observed_at_ms ?? record.created_at_ms;
            const caseState =
              intent?.case_state ?? (entry.kind === "case" ? entry.value.state : "—");
            return (
              <article className="trading-case-row" key={record.case_id}>
                <span>{caseClock(observedAt)}</span>
                <span>{strategyCaseLabel(record.strategy_id)}</span>
                <span>{record.regime ? (REGIME_ZH[record.regime] ?? record.regime) : "—"}</span>
                <span>{CASE_STATE_ZH[caseState] ?? caseState}</span>
                <span>{record.policy_reason ? policyRuleZh(record.policy_reason) : "—"}</span>
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
          {query.isError && !query.data
            ? "资本通道账本本轮不可用；不能据此断言没有案例。"
            : query.data == null
              ? "正在读取资本通道账本…"
              : "当前窗口没有这个代币的 Case 或 Intent。"}
        </TradingEmptyNote>
      )}
      <TradingSourceLine path="GET /api/trading/intents?underlying={base} → intents[] · cases_without_intents[]" />
    </Card>
  );
}
