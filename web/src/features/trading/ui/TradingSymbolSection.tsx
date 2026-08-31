import { Card } from "@shared/ui/Card";
import * as PageState from "@shared/ui/PageState";

import { useTradingCasesWithToken, useTradingSignalsWithToken } from "../api/tradingQueries";
import { caseStateLabel } from "../model/tradingCases";
import { caseClock, policyLabel, policyReasonLabel } from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

import "./trading.css";
import "./tradingSymbolSection.css";

/** One token's current Alpha history: frozen Cases and their engine-neutral Signals. */
export function TradingSymbolSection({ base, token }: { base: string; token: string }) {
  const casesQuery = useTradingCasesWithToken(token, base);
  const market = `crypto:perp:${base}:USDT`;
  const signalsQuery = useTradingSignalsWithToken(token, market);
  const signalsByCase = new Map(
    (signalsQuery.data?.signals ?? []).map((signal) => [signal.case_id, signal]),
  );
  const signalLedgerAvailable = Boolean(signalsQuery.data);
  const rows = [...(casesQuery.data?.cases ?? [])].sort(
    (a, b) => b.created_at_ms - a.created_at_ms,
  );

  return (
    <Card
      flush
      hint={`过去 ${casesQuery.data?.window_hours ?? 24} 小时；Case 与 Signal 分别读取`}
      title="Alpha 复盘 · Case → Signal"
    >
      {rows.length ? (
        <PageState.Stale
          failedRefresh={
            signalsQuery.isError && !signalLedgerAvailable
              ? "Signal 账本本轮不可用；不能据此断言未发出 Signal。"
              : undefined
          }
          onRetry={
            signalsQuery.isError && !signalLedgerAvailable
              ? () => void signalsQuery.refetch()
              : undefined
          }
          updating={signalsQuery.isFetching}
        >
          <div className="trading-table">
            <div aria-hidden className="trading-case-head">
              <span>CUTOFF</span>
              <span>策略</span>
              <span>Case</span>
              <span>规则</span>
              <span>Signal</span>
              <span>TTL</span>
            </div>
            {rows.map((record) => {
              const signal = signalsByCase.get(record.case_id);
              const signalLabel = signal
                ? `${signal.direction.toUpperCase()} · seq ${signal.seq}`
                : signalLedgerAvailable
                  ? "未发出"
                  : signalsQuery.isError
                    ? "账本不可用"
                    : "读取中";
              const ttlLabel = signal
                ? signal.expired
                  ? "已过期"
                  : "有效"
                : signalLedgerAvailable
                  ? "—"
                  : "未知";
              return (
                <article className="trading-case-row" key={record.case_id}>
                  <span>{caseClock(record.observed_at_ms)}</span>
                  <span>{policyLabel(record.policy_id)}</span>
                  <span>{caseStateLabel(record)}</span>
                  <span>{policyReasonLabel(record.policy_reason)}</span>
                  <span>{signalLabel}</span>
                  <span>{ttlLabel}</span>
                </article>
              );
            })}
          </div>
        </PageState.Stale>
      ) : (
        <TradingEmptyNote>
          {casesQuery.isError && !casesQuery.data
            ? "Alpha Case 账本本轮不可用；不能据此断言没有案例。"
            : casesQuery.isPending
              ? "正在读取 Alpha Case 账本…"
              : "当前窗口没有这个代币的 Case。"}
        </TradingEmptyNote>
      )}
      <TradingSourceLine path="GET /api/trading/cases?underlying={base} → cases[] · GET /api/trading/signals?market={market_key} → signals[]" />
    </Card>
  );
}
