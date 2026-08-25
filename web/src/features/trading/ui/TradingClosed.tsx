import { newsSymbolPath } from "@shared/routing/paths";
import { Card } from "@shared/ui/Card";
import { Link } from "react-router-dom";

import type { TradingOrder } from "../api/tradingQueries";
import { CASE_KIND_ZH, heldFor } from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

/**
 * What closed, and how it closed — from the ledger, not from a local candle.
 *
 * `exit_reason` is the account of the close the reconciler wrote when it saw the venue's evidence. The
 * console does not decide that a stop "must have" triggered because a price passed through it; a position
 * closes when the ledger says a close was observed.
 *
 * `realized_bps` is only present when the exit was measured. An operator-resolved close moved a position but
 * nobody computed a return for it, so it renders `—` rather than a zero that would drag an average.
 */
export function TradingClosed({ rows }: { rows: readonly TradingOrder[] }) {
  return (
    <Card flush hint="平仓证据来自账本状态，不是本地蜡烛" title={`今日已了结 · ${rows.length}`}>
      {rows.length === 0 ? (
        <TradingEmptyNote>过去 24 小时没有了结的仓位。</TradingEmptyNote>
      ) : (
        <div className="trading-table">
          <div aria-hidden className="trading-closed-head">
            <span>标的</span>
            <span>案例</span>
            <span>入场 → 出场</span>
            <span>了结方式</span>
            <span>持有</span>
            <span className="trading-num">结果</span>
          </div>
          {rows.map((order) => (
            <article className="trading-closed-row" key={order.order_id}>
              <span className="trading-symbol">
                <Link to={newsSymbolPath(order.base_symbol)}>{order.base_symbol}</Link>
              </span>
              <span className="trading-kind">
                {CASE_KIND_ZH[order.case_kind] ?? order.case_kind}
              </span>
              <span className="trading-num">
                {order.average_price ?? order.entry_reference} → {order.exit_price ?? "—"}
              </span>
              <span className="trading-note">{order.exit_reason ?? "—"}</span>
              <span className="trading-num">{heldFor(order)}</span>
              <span className="trading-num">
                {order.realized_bps == null ? (
                  <span className="trading-unmeasured" title="出场未被测量，不进入已实现口径">
                    —
                  </span>
                ) : (
                  <b data-tone={order.realized_bps >= 0 ? "up" : "down"}>
                    {order.realized_bps > 0 ? "+" : ""}
                    {(order.realized_bps / 100).toFixed(2)}%
                  </b>
                )}
              </span>
            </article>
          ))}
        </div>
      )}
      <TradingSourceLine
        note="只有被测量的出场进入已实现口径：操作员解决的平仓移动了仓位，但没人为它算过收益"
        path="GET /api/trading/orders → orders[] where state = CLOSED"
      />
    </Card>
  );
}
