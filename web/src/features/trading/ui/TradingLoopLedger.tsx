import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { SourceLine } from "@shared/ui/SourceLine";
import { useSearchParams } from "react-router-dom";

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

/** One venue-derived entry per row. Attribution and frozen risk open by entry identity. */
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
  const [params, setParams] = useSearchParams();
  const selectedEntry = params.get("entry");
  const manual = rows.filter((row) => row.source === "manual").length;
  return (
    <Card
      data-block="ledger"
      flush
      hint={`所列入场 ${rows.length}（Signal ${rows.length - manual} · 手工 ${manual}）`}
      title={caseFiltered ? "关联执行记录" : "执行记录 · 近 24 小时及未结束交易"}
    >
      {selectedEntry && !rows.some((row) => row.entry_id === selectedEntry) && !pending ? (
        <EmptyNote>当前读取范围内未找到这笔执行；不能据此断言它没有发生。</EmptyNote>
      ) : null}
      {rows.length ? (
        <div className="trading-ledger-table">
          <div aria-hidden className="trading-ledger-head">
            <span>时间 / 市场</span>
            <span>阶段 / 处置</span>
            <span>成交数量 / 入场均价</span>
            <span>净收益</span>
            <span>依据</span>
          </div>
          {rows.map((row) => {
            const open = selectedEntry === row.entry_id;
            return (
              <article className="trading-ledger-row" key={row.entry_id}>
                <div className="trading-ledger-main">
                  <span className="trading-ledger-market" data-label="时间 / 市场">
                    {row.case_id ? (
                      <button
                        aria-expanded={row.case_id === selectedCaseId}
                        className="trading-case-link"
                        onClick={() => onOpenCase(row.case_id!)}
                        type="button"
                      >
                        {row.market_key}
                      </button>
                    ) : (
                      <b>{row.market_key}</b>
                    )}
                    <small>{nsClock(row.observed_at_ns)}</small>
                    <small data-tone={row.direction === "long" ? "long" : "short"}>
                      {row.direction.toUpperCase()} ·{" "}
                      {EXECUTION_SOURCE_ZH[row.source] ?? row.source}
                    </small>
                  </span>
                  <span data-label="阶段 / 处置">
                    <b className="trading-stage" data-stage={row.stage}>
                      {EXECUTION_STAGE_ZH[row.stage] ?? row.stage}
                    </b>
                    <small>{signalDispositionLabel(row.disposition_reason)}</small>
                    {row.order_reject_reason ? (
                      <small data-tone="caution">{row.order_reject_reason}</small>
                    ) : null}
                  </span>
                  <span data-label="成交数量 / 入场均价">
                    <b>{row.fill_quantity ?? "—"}</b>
                    <small>入场 {row.fill_avg_price ?? "—"}</small>
                  </span>
                  <span data-label="净收益">
                    <b data-tone={moneyTone(row.net_pnl_usd)}>
                      {row.net_known
                        ? moneyLabel(row.net_pnl_usd)
                        : row.stage === "closed"
                          ? "净收益未知"
                          : "—"}
                    </b>
                    <small>
                      持仓 {holdingLabel(row.entry_filled_at_ns, row.position_closed_at_ns)}
                    </small>
                  </span>
                  <ActionButton
                    size="sm"
                    aria-expanded={open}
                    onClick={() => {
                      const next = new URLSearchParams(params);
                      if (open) next.delete("entry");
                      else next.set("entry", row.entry_id);
                      setParams(next, { replace: true });
                    }}
                  >
                    {open ? "收起明细" : "执行明细"}
                  </ActionButton>
                </div>
                {open ? <ExecutionDetail row={row} onOpenCase={onOpenCase} /> : null}
              </article>
            );
          })}
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

function ExecutionDetail({
  row,
  onOpenCase,
}: {
  row: TradingExecutionRow;
  onOpenCase: (caseId: string) => void;
}) {
  return (
    <section className="trading-entry-detail" aria-label={`执行明细 ${row.market_key}`}>
      <div>
        <h3>冻结计划与退出</h3>
        <dl>
          <div>
            <dt>止损价</dt>
            <dd>{row.stop_trigger_price ?? "—"}</dd>
          </div>
          {row.stop_distance_bps != null ? (
            <div>
              <dt>止损距离</dt>
              <dd>冻结止损 {row.stop_distance_bps} bps</dd>
            </div>
          ) : null}
          {row.risk_budget_usd != null ? (
            <div>
              <dt>风险预算</dt>
              <dd>
                风险预算 {moneyLabel(row.risk_budget_usd)} · 杠杆上限 {row.max_leverage_at_creation}
                ×
              </dd>
            </div>
          ) : null}
          <div>
            <dt>退出价</dt>
            <dd>{row.exit_price ?? "—"}</dd>
          </div>
          {row.take_profit_trigger_price != null ? (
            <div>
              <dt>止盈价</dt>
              <dd>{row.take_profit_trigger_price}</dd>
            </div>
          ) : null}
          {row.take_profit_bps != null && row.max_holding_ns != null ? (
            <div>
              <dt>退出条件</dt>
              <dd>
                止盈 {row.take_profit_bps} bps · 最长 {holdingLabel(0, row.max_holding_ns)}
              </dd>
            </div>
          ) : null}
          {row.exit_reason ? (
            <div>
              <dt>退出原因</dt>
              <dd>{EXIT_REASON_ZH[row.exit_reason] ?? row.exit_reason}</dd>
            </div>
          ) : null}
        </dl>
      </div>
      <div>
        <h3>收益归因</h3>
        <dl>
          <div>
            <dt>手续费后已实现</dt>
            <dd>{row.realized_pnl_usd == null ? "未取得" : moneyLabel(row.realized_pnl_usd)}</dd>
          </div>
          <div>
            <dt>手续费（已扣除）</dt>
            <dd>{row.fees_usd == null ? "未取得" : moneyLabel(row.fees_usd)}</dd>
          </div>
          <div>
            <dt>资金费</dt>
            <dd>{row.funding_usd == null ? "未取得完整归因" : moneyLabel(row.funding_usd)}</dd>
          </div>
          <div>
            <dt>净收益</dt>
            <dd>
              {row.net_known
                ? moneyLabel(row.net_pnl_usd)
                : row.stage === "closed"
                  ? "净收益未知"
                  : "尚无已知平仓净收益"}
            </dd>
          </div>
        </dl>
        <p>只有资金费覆盖完整且归因唯一时才展示净收益；已知部分不代表完整结果。</p>
      </div>
      <div className="trading-entry-identity">
        <code>{row.entry_id}</code>
        {row.case_id ? (
          <ActionButton size="sm" onClick={() => onOpenCase(row.case_id!)}>
            查看来源策略判定
          </ActionButton>
        ) : (
          <small>没有记录关联 Case 身份</small>
        )}
      </div>
    </section>
  );
}
