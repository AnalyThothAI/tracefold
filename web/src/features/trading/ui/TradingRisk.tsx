import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { Metric, MetricRow } from "@shared/ui/Metric";
import { SourceLine } from "@shared/ui/SourceLine";
import type { ReactNode } from "react";

import type { TradingExecutionReadiness } from "../api/tradingQueries";
import {
  bpsPercent,
  entryBlockReasonLabel,
  moneyLabel,
  orderLegLabel,
  protectionStatusLabel,
} from "../model/tradingLabels";

/** Read the single current-state contract, keeping expired observations explicitly historical. */
export function TradingSafetyStrip({
  execution,
  stale,
}: {
  execution: TradingExecutionReadiness;
  stale: boolean;
}) {
  return (
    <div className="trading-risk" data-block="safety">
      {/*
       * Two words. `当前仓位可保护 / 退出` was the third and answered `execution_safe`, a claim about the
       * Runtime's private account proof; Nautilus owns execution state now and reconciles the venue itself
       * (#680), so the proof and the tile went together. What remains is whether the process is alive and
       * whether it will take a new entry — and if not, the reason it names.
       */}
      <MetricRow className="trading-safety-grid" columns={2} label="执行安全状态">
        <Metric
          eyebrow="执行状态通道"
          value={safety(execution.alive, stale)}
          caption="Runtime 心跳经数据库与 HTTP 发布"
          tone={!stale && execution.alive ? "accent" : "caution"}
        />
        <Metric
          eyebrow="允许新增仓位"
          value={safety(execution.entries_armed, stale)}
          caption={stale ? "等待新状态" : entryBlockReasonLabel(execution.entry_block_reason)}
          tone={!stale && execution.entries_armed ? "accent" : "caution"}
        />
      </MetricRow>
      {stale ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          状态通道失联：未取得有效期内的新状态，无法确认 Runtime 当前运行情况；下方保留上次观察。
        </p>
      ) : null}
      {!stale && execution.account_projection_failure ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          执行服务仍在发布心跳；账户投影失败（{execution.account_projection_failure}
          ），账户资料取自上次成功观察。
        </p>
      ) : null}
      {!stale && execution.convergence_failure ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          认领检查失败（{execution.convergence_failure}）；上次检查结论仍保留，新增仓位待重新核实。
        </p>
      ) : null}
      {!stale && execution.venue_read_failure ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          最近一次场所读取失败（{execution.venue_read_failure}
          ）；上次成功的场所证据不会被当作新观察。
        </p>
      ) : null}
      {!stale && execution.recovery_result ? (
        <p
          role="status"
          className="trading-alert-line"
          data-tone={execution.recovery_result === "succeeded" ? "neutral" : "caution"}
        >
          原生对账：
          {execution.recovery_result === "running"
            ? "进行中"
            : execution.recovery_result === "succeeded"
              ? "已完成，等待新的场所证据确认"
              : "未收敛，保留风险提示"}
        </p>
      ) : null}
      <p className="trading-routes-line">
        可执行市场 {execution.routes_count} 个 · 账户槽位 <code>{execution.account_slot}</code>
      </p>
    </div>
  );
}

/** Account rows, Plan association and venue differences from one Runtime observation. */
export function TradingExposure({
  execution,
  stale,
}: {
  execution: TradingExecutionReadiness;
  stale: boolean;
}) {
  const account = execution.current_account;
  const positions = account?.positions ?? [];
  const orders = account?.orders ?? [];
  const findings = account?.findings ?? [];
  const open =
    (account?.positions_total ?? 0) > 0 || orders.length > 0 || execution.unexpected_exposure;
  return (
    <Card data-block="exposure" flush title="当前仓位与保护">
      <details className="trading-exposure" open={open}>
        <summary>
          <span>
            仓位 {account?.positions_total ?? "—"} · 挂单 {account?.open_orders_count ?? "—"} · 保护{" "}
            {stale ? "待确认" : protectionStatusLabel(execution.protection_status)}
          </span>
          <small>
            {open
              ? stale
                ? "上次观察的仓位与订单"
                : "最近观察的仓位与订单"
              : account == null
                ? "未取得 Runtime 账户快照"
                : stale
                  ? "上次读取未见仓位"
                  : "Runtime 当前未见仓位"}
          </small>
        </summary>

        {execution.unexpected_exposure ? (
          <div className="trading-alert-line" data-tone="alert">
            <b>
              {stale ? "上次检查发现异常；最新状态未取得。" : "最近检查发现异常；新增仓位受阻。"}
            </b>
            {findings.length ? (
              <ul>
                {findings.map((finding) => (
                  <li key={finding.kind + finding.object_id}>
                    {findingLabel(finding.kind)}：{finding.instrument_id} · {finding.object_id}
                    {finding.plan_entry_id ? " · Plan " + finding.plan_entry_id : ""}
                    {finding.venue_quantity != null || finding.cache_quantity != null
                      ? " · 场所 " +
                        (finding.venue_quantity ?? "未知") +
                        " / 本地 " +
                        (finding.cache_quantity ?? "未知")
                      : ""}
                  </li>
                ))}
              </ul>
            ) : (
              <p>旧检查未保存对象明细；需要 Runtime 的新检查。</p>
            )}
            {account && account.findings_total > findings.length ? (
              <p>另有 {account.findings_total - findings.length} 项未在本页展开。</p>
            ) : null}
          </div>
        ) : null}
        {account &&
        (account.positions_total > positions.length || account.orders_total > orders.length) ? (
          <p className="trading-alert-line" data-tone="caution">
            账户列表已截断：仓位 {positions.length}/{account.positions_total}，订单 {orders.length}/
            {account.orders_total}。
          </p>
        ) : null}

        <p className="trading-routes-line">
          账户采样 {observedTime(account?.observed_at_ms)} · 认领检查{" "}
          {observedTime(execution.convergence_checked_at_ms)} · 上次成功场所读取{" "}
          {observedTime(execution.venue_read_completed_at_ms)}
        </p>
        <div className="trading-fact-grid">
          <Fact label="账户权益" value={moneyLabel(account?.equity_usd)} />
          <Fact
            label="当日回撤"
            value={
              account?.daily_drawdown_usd == null
                ? "未取得"
                : `${moneyLabel(account.daily_drawdown_usd)} · ${bpsPercent(account.daily_drawdown_bps)}`
            }
            warn={Number(account?.daily_drawdown_usd ?? 0) > 0}
          />
          <Fact
            label="账户字段"
            value={account?.complete ? "字段完整" : account ? "部分字段缺失" : "未取得"}
            warn={!account?.complete}
          />
          <Fact label="在途订单" value={account?.inflight_orders_count ?? "—"} />
        </div>

        {positions.length ? (
          <div className="trading-position-list">
            {positions.map((position) => {
              const guarded = position.protection_status === "protected";
              return (
                <article className="trading-position-row" key={position.position_id}>
                  <div className="trading-position-identity">
                    <b>{position.instrument_id}</b>
                    <span data-tone={position.side === "long" ? "long" : "short"}>
                      {position.side === "long" ? "多仓" : "空仓"}
                    </span>
                    {position.source === "venue" ? (
                      <span data-tone="caution">上次场所观察</span>
                    ) : null}
                    {position.plan_entry_id ? <span>Plan {position.plan_entry_id}</span> : null}
                    {!position.owned ? <span data-tone="alert">计划关联待核实</span> : null}
                  </div>
                  <div className="trading-position-facts">
                    <Fact label="数量" value={position.quantity} />
                    <Fact label="入场均价" value={position.entry_price ?? "未取得"} />
                    <Fact label="标记价格" value={position.mark_price ?? "未取得"} />
                    <Fact
                      label="未实现盈亏"
                      value={moneyLabel(position.unrealized_pnl_usd)}
                      warn={position.unrealized_pnl_usd == null}
                    />
                  </div>
                  <div
                    className="trading-protection-strip"
                    data-tone={!stale && guarded ? "protected" : "caution"}
                  >
                    <b>
                      {stale
                        ? "上次观察的保护；当前未确认"
                        : protectionStatusLabel(position.protection_status)}
                    </b>
                    <span>止损 {position.stop_trigger_price ?? "未挂"}</span>
                    <span>止盈 {position.take_profit_trigger_price ?? "未挂"}</span>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <EmptyNote className="trading-empty-note">
            {account == null
              ? "未取得 Runtime 账户快照，不能据此断言没有仓位。"
              : stale
                ? "上次读取时 Runtime 未见仓位；当前状态待确认。"
                : "Runtime 当前未见仓位。"}
          </EmptyNote>
        )}

        {orders.length ? (
          <div className="trading-current-order-list">
            {orders.map((order) => (
              <article className="trading-current-order-row" key={order.client_order_id}>
                <b>{order.instrument_id}</b>
                <span>{order.state.toUpperCase()}</span>
                <span data-tone={order.leg === "unknown" ? "caution" : undefined}>
                  {orderLegLabel(order.leg)} · Qty {order.quantity}
                </span>
                <span>Trigger {order.trigger_price ?? "—"}</span>
                <span data-tone={!order.owned ? "caution" : undefined}>
                  {order.owned ? "关联计划" : "计划关联待核实"}
                  {order.reduce_only ? " · REDUCE ONLY" : ""}
                </span>
              </article>
            ))}
          </div>
        ) : (
          <p className="trading-inline-empty">
            {account == null
              ? "未取得挂单与在途订单。"
              : stale
                ? "上次读取时未见挂单或在途订单。"
                : "Runtime 当前未见挂单或在途订单。"}
          </p>
        )}
      </details>
      <SourceLine path="GET /api/trading/status → execution.current_account / findings" />
    </Card>
  );
}

function Fact({ label, value, warn = false }: { label: string; value: ReactNode; warn?: boolean }) {
  return (
    <span className="trading-fact" data-tone={warn ? "caution" : undefined}>
      <small>{label}</small>
      <b>{value}</b>
    </span>
  );
}

function safety(value: boolean, stale: boolean): string {
  if (stale) return "待确认";
  return value ? "是" : "否";
}

function findingLabel(kind: string): string {
  const labels: Record<string, string> = {
    unclaimed_position: "未认领仓位",
    unexpected_order: "非预期订单",
    ownership_mismatch: "计划与仓位身份不符",
    venue_cache_mismatch: "场所与本地数量不符",
    close_unconfirmed: "平仓尚未确认",
    ambiguous: "多个计划可能关联",
  };
  return labels[kind] ?? kind;
}

function observedTime(value: number | null | undefined): string {
  return value == null ? "未取得" : new Date(value).toLocaleString("zh-CN");
}
