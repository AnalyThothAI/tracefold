import { newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Card } from "@shared/ui/Card";
import * as PageState from "@shared/ui/PageState";
import { Check } from "lucide-react";
import { Link } from "react-router-dom";

import type { TradingOrder } from "../api/tradingQueries";
import {
  holdRemaining,
  ORDER_STATE_NOTE,
  pnlLabel,
  strategyCaseLabel,
  stopVerified,
} from "../model/tradingLabels";

import { TradingEmptyNote, TradingInvariantLine } from "./TradingChrome";

/**
 * Everything that holds, or may yet turn out to hold, exposure — and what each row has actually proven.
 *
 * The ambiguous states are in here on purpose. An order whose provider write is unresolved is *more*
 * dangerous than one that is merely open: nobody knows whether it filled, and it is holding the symbol's
 * slot until a read or a human says otherwise. Filtering the list down to "real" positions would hide the
 * rows that most need an operator's eye.
 *
 * There is no action on any row. The three operator mutations — approve, reject, resolve — stay on the CLI,
 * where they run as `workers`; `tracefold_serve` carries `default_transaction_read_only = on` precisely so
 * an internet-facing surface cannot reach them.
 */
export function TradingExposure({
  count,
  error,
  loading,
  mode,
  onRetry,
  rows,
}: {
  count: number;
  error: unknown;
  loading: boolean;
  mode: string;
  onRetry: () => void;
  rows: readonly TradingOrder[];
}) {
  const referrer = useRouteReferrer();
  const nowMs = Date.now();
  const rowModes = new Set(rows.map((row) => row.mode));
  const displayedMode = rows.length === 0 ? mode : rowModes.size === 1 ? [...rowModes][0] : "mixed";
  return (
    <Card
      flush
      hint="ACK 不是成交，成交不是保护——每行只声称已证明的那一步"
      title={`当前暴露 · ${count}`}
    >
      {error ? <PageState.Error error={error} onRetry={onRetry} /> : null}
      {!error && loading && rows.length === 0 ? (
        <PageState.Loading label="正在读取当前暴露" layout="panel" rows={3} />
      ) : null}
      {!error && !loading && rows.length === 0 ? (
        <TradingEmptyNote>当前没有任何持仓或未决意图。</TradingEmptyNote>
      ) : null}

      {!error && rows.length > 0 ? (
        <div className="trading-table">
          <div aria-hidden className="trading-exposure-head">
            <span>标的 · 方向</span>
            <span>案例</span>
            <span>订单状态</span>
            <span>数量 @ 入场</span>
            <span>原生止损</span>
            <span>剩余持有</span>
            <span>状态说明</span>
            <span>{pnlLabel(displayedMode)}</span>
          </div>
          {rows.map((order) => (
            <article
              className="trading-exposure-row"
              data-ambiguous={order.state === "AMBIGUOUS" || undefined}
              key={order.order_id}
            >
              <span className="trading-symbol">
                <Link state={referrer} to={newsSymbolPath(order.base_symbol)}>
                  {order.base_symbol}
                </Link>
                <small data-side={order.side}>{order.side === "buy" ? "多" : "空"}</small>
              </span>
              <span className="trading-kind" title={`strategy_id: ${order.strategy_id}`}>
                {strategyCaseLabel(order.strategy_id)}
              </span>
              {/* The ledger's own word, never translated into 已成交. */}
              <span className="trading-state" data-state={order.state}>
                {order.state}
              </span>
              <span className="trading-num">
                {order.filled_quantity ?? `拟 ${order.quantity}`} @{" "}
                {order.average_price ?? order.entry_reference}
              </span>
              <span className="trading-num trading-stop">
                {order.stop_price}
                {/*
                 * The tick means a read proved a reduce-only stop covering the filled quantity — which is
                 * exactly what `OPEN` means. A stop *price* exists on every prepared order and proves
                 * nothing, so it never earns the mark.
                 */}
                {stopVerified(order) ? <Check aria-label="原生止损已验证" /> : null}
              </span>
              <span className="trading-num">{holdRemaining(order, nowMs)}</span>
              <span className="trading-note">
                {order.state_reason ?? ORDER_STATE_NOTE[order.state] ?? ""}
              </span>
              <span className="trading-num trading-unmeasured">—</span>
            </article>
          ))}
        </div>
      ) : null}

      <TradingInvariantLine>
        一个 source fact 至多一个案例 · 一个案例至多一笔意图 · provider_attempt_count ≤ 1 ·
        歧义后只读对账，永不盲重发
      </TradingInvariantLine>
    </Card>
  );
}
