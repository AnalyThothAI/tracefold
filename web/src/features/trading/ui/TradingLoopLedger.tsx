import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { SourceLine } from "@shared/ui/SourceLine";

import type { TradingExecutionRow } from "../api/tradingQueries";
import {
  EXECUTION_SOURCE_ZH,
  EXECUTION_STAGE_ZH,
  EXIT_REASON_ZH,
  holdingLabel,
  ledgerSentence,
  moneyLabel,
  moneyTone,
  nsClock,
  signalDispositionLabel,
} from "../model/tradingLabels";

/**
 * ④ The loop ledger: one row per entry, from the Signal's clock to the realized number (#604 T4).
 *
 * Nine columns where there were twelve. `方向` was its own column and has been `LONG` on every production
 * row since the lane started; it is a chip inside the market cell now, which is where a reader looks for
 * it anyway. `处置` and `阶段` were both washed amber for the same refusal — the stage keeps the colour and
 * the disposition keeps the words. What the three freed columns paid for is the fact this chain is
 * sharpest about and had nowhere to say: **how long the position was actually held**, from the fill clock
 * to the close clock, under the realized number it produced.
 *
 * That number is coloured now. `−$11.04` and `+$110.33` scanned identically in `--text-secondary`, and a
 * realized result belongs to the market axis this console already owns — red for a profit, green for a
 * loss, exactly as `tokens.css` reads the two directions.
 *
 * Nothing here is derived except the holding interval, which is one subtraction of two clocks the server
 * stores. `stage` is the server's word, the prices and quantities are the venue's own decimal strings, and
 * `order_reject_reason` is printed verbatim under the disposition: it is the venue talking, and
 * translating it would put words in the exchange's mouth.
 */
export function TradingLoopLedger({
  caseFiltered = false,
  complete,
  failed,
  onOpenCase,
  pending,
  rows,
  selectedCaseId,
}: {
  caseFiltered?: boolean;
  complete: boolean;
  failed: boolean;
  onOpenCase: (caseId: string) => void;
  pending: boolean;
  rows: readonly TradingExecutionRow[];
  selectedCaseId: string | null;
}) {
  const manual = rows.filter((row) => row.source === "manual").length;
  return (
    <Card
      data-block="ledger"
      flush
      hint={`入场 ${rows.length}（Signal ${rows.length - manual} · 手工 ${manual}）`}
      title={caseFiltered ? "关联执行记录" : "执行记录 · 最近 24 小时"}
    >
      {rows.length ? (
        <div className="trading-ledger-table">
          <div aria-hidden className="trading-ledger-head">
            <span>时间</span>
            <span>市场</span>
            <span>处置</span>
            <span>阶段</span>
            <span>成交量</span>
            <span>入场均价</span>
            <span>止损价</span>
            <span>退出</span>
            <span>已实现</span>
          </div>
          {rows.map((row) => (
            <article className="trading-ledger-row" key={row.entry_id}>
              <span data-label="时间">{nsClock(row.observed_at_ns)}</span>
              <span className="trading-ledger-market" data-label="市场">
                {row.case_id ? (
                  <button
                    aria-expanded={row.case_id === selectedCaseId}
                    className="trading-case-link"
                    onClick={() => onOpenCase(row.case_id as string)}
                    type="button"
                  >
                    {row.market_key}
                  </button>
                ) : (
                  <b>{row.market_key}</b>
                )}
                <small data-tone={row.direction === "long" ? "long" : "short"}>
                  {row.direction.toUpperCase()} · {EXECUTION_SOURCE_ZH[row.source] ?? row.source}
                </small>
              </span>
              <span data-label="处置">
                {signalDispositionLabel(row.disposition_reason)}
                {row.order_reject_reason ? (
                  <small data-tone="caution">{row.order_reject_reason}</small>
                ) : null}
              </span>
              <span data-label="阶段">
                <b className="trading-stage" data-stage={row.stage}>
                  {EXECUTION_STAGE_ZH[row.stage] ?? row.stage}
                </b>
              </span>
              <span data-label="成交量">{row.fill_quantity ?? "—"}</span>
              <span data-label="入场均价">{row.fill_avg_price ?? "—"}</span>
              <span data-label="止损价">{row.stop_trigger_price ?? "—"}</span>
              <span data-label="退出">
                {row.exit_price ?? "—"}
                {row.exit_reason ? (
                  <small>{EXIT_REASON_ZH[row.exit_reason] ?? row.exit_reason}</small>
                ) : null}
              </span>
              <span data-label="已实现">
                <b data-tone={moneyTone(row.realized_pnl_usd)}>
                  {moneyLabel(row.realized_pnl_usd)}
                </b>
                <small>
                  持仓 {holdingLabel(row.entry_filled_at_ns, row.position_closed_at_ns)}
                </small>
              </span>
            </article>
          ))}
        </div>
      ) : (
        <EmptyNote className="trading-empty-note">
          {caseFiltered && !failed && !pending
            ? "该策略判定没有保留的执行记录。"
            : ledgerSentence({ failed, pending, subject: "执行" })}
        </EmptyNote>
      )}
      {rows.length && !complete ? (
        <EmptyNote className="trading-empty-note">
          本窗口已截断；未列出的入场不能解释为没有发生。
        </EmptyNote>
      ) : null}
      <SourceLine path="GET /api/trading/executions → executions[]" />
    </Card>
  );
}
