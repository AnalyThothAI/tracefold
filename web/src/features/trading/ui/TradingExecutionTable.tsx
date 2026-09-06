import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { SourceLine } from "@shared/ui/SourceLine";

import type { TradingExecutionRow } from "../api/tradingQueries";
import {
  EXECUTION_SOURCE_ZH,
  EXECUTION_STAGE_ZH,
  EXIT_REASON_ZH,
  moneyLabel,
  nsClock,
  signalDispositionLabel,
} from "../model/tradingLabels";

import { TradingLedgerNote } from "./TradingChrome";

/**
 * CONFIRM: one row per entry, every cell a field `GET /api/trading/executions` already folded (#528).
 *
 * An entry is a Signal or a manual entry the operator typed, and `source` is the only column that tells
 * them apart: a manual entry has no Case, and every other fact it carries is the same venue fact folded
 * the same way (#528 PR-3). Nothing here is derived. `stage` is the server's word, the two prices and the
 * quantity are the venue's own decimal strings, and the realized PnL is the one Nautilus reported on
 * `PositionClosed` — the total above the table is their sum and the only arithmetic on this page, because
 * #528 explicitly refuses an equity curve table for a number that is already one column.
 *
 * Two columns went in #537 PR-5. `disposition` was `accepted` / `rejected` beside a stage that already
 * says `ordered` or `rejected` about the same row, and `position_status` was `closed` beside
 * `stage=closed`. A Signal row's `case_id` is a link now: the Case that authored it opens in the drawer
 * on this page rather than in a list of every Case below it.
 */
export function TradingExecutionTable({
  complete,
  failed,
  onOpenCase,
  pending,
  rows,
  selectedCaseId,
}: {
  complete: boolean;
  failed: boolean;
  onOpenCase: (caseId: string) => void;
  pending: boolean;
  rows: readonly TradingExecutionRow[];
  selectedCaseId: string | null;
}) {
  const filled = rows.filter((row) => row.fill_quantity != null).length;
  const manual = rows.filter((row) => row.source === "manual").length;
  const realized = rows.reduce((sum, row) => sum + realizedPnl(row), 0);
  return (
    <Card
      flush
      hint={`入场 ${rows.length}（Signal ${rows.length - manual} · 手工 ${manual}）· 执行 ${filled} · 已实现 ${moneyLabel(realized.toFixed(2))}`}
      title="今日执行"
    >
      {rows.length ? (
        <div className="trading-execution-table">
          <div aria-hidden className="trading-execution-head">
            <span>时间</span>
            <span>来源</span>
            <span>市场</span>
            <span>方向</span>
            <span>处置</span>
            <span>阶段</span>
            <span>成交量</span>
            <span>成交均价</span>
            <span>止损价</span>
            <span>退出价</span>
            <span>已实现</span>
            <span>退出原因</span>
          </div>
          {rows.map((row) => (
            <article className="trading-execution-row" key={row.entry_id}>
              <span data-label="时间">{nsClock(row.observed_at_ns)}</span>
              <span data-label="来源">
                {row.case_id ? (
                  <button
                    aria-expanded={row.case_id === selectedCaseId}
                    className="trading-case-link"
                    onClick={() => onOpenCase(row.case_id as string)}
                    type="button"
                  >
                    {EXECUTION_SOURCE_ZH[row.source] ?? row.source}
                  </button>
                ) : (
                  (EXECUTION_SOURCE_ZH[row.source] ?? row.source)
                )}
              </span>
              <span data-label="市场">{row.market_key}</span>
              <span data-label="方向" data-tone={row.direction === "long" ? "long" : "short"}>
                {row.direction.toUpperCase()}
              </span>
              <span
                data-label="处置"
                data-tone={REFUSED_STAGES.has(row.stage) ? "caution" : undefined}
              >
                {signalDispositionLabel(row.disposition_reason)}
              </span>
              <span className="trading-stage" data-label="阶段" data-stage={row.stage}>
                {EXECUTION_STAGE_ZH[row.stage] ?? row.stage}
              </span>
              <span data-label="成交量">{row.fill_quantity ?? "—"}</span>
              <span data-label="成交均价">{row.fill_avg_price ?? "—"}</span>
              <span data-label="止损价">{row.stop_trigger_price ?? "—"}</span>
              <span data-label="退出价">{row.exit_price ?? "—"}</span>
              <span data-label="已实现">{moneyLabel(row.realized_pnl_usd)}</span>
              <span data-label="退出原因">
                {row.exit_reason ? (EXIT_REASON_ZH[row.exit_reason] ?? row.exit_reason) : "—"}
              </span>
            </article>
          ))}
        </div>
      ) : (
        <TradingLedgerNote failed={failed} pending={pending} subject="执行" />
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

/** The two stages that mean the entry never reached the venue, as `tracefold/trading/stages.py` derives them. */
const REFUSED_STAGES = new Set(["rejected", "expired"]);

function realizedPnl(row: TradingExecutionRow): number {
  const value = Number(row.realized_pnl_usd);
  return Number.isFinite(value) ? value : 0;
}
