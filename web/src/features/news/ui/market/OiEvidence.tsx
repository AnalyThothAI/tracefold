import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import type { NewsMarketObservation } from "../../api/newsQueries";
import { oiWindowLabel } from "../../model/marketFacts";
import { formatBps, formatPrice } from "../../model/newsPrice";

export function MarketObservationMetrics({
  observation: o,
}: {
  observation: NewsMarketObservation;
}) {
  if (o.market_kind === "oi" && o.parse_status === "parsed")
    return (
      <span className="news-oi-observation-metrics">
        <span>
          <small>OI 本次变化</small>
          <b>{formatBps(o.oi_change_bps)}</b>
          <small>{oiWindowLabel(o)}</small>
        </span>
        <span>
          <small>OI 名义价值 · USD</small>
          <b>{o.oi_value_usd == null ? "未取得" : `$${o.oi_value_usd.toLocaleString("en-US")}`}</b>
        </span>
        <span>
          <small>场所与原始合约</small>
          <b>{o.source_venue ?? "场所未确认"}</b>
          <small>{o.raw_instrument ?? "合约未记录"}</small>
        </span>
      </span>
    );
  if (o.market_kind === "liquidation" && o.parse_status === "parsed")
    return (
      <span className="news-market-title">
        {o.liquidated_position_side === "long"
          ? "多仓强平"
          : o.liquidated_position_side === "short"
            ? "空仓强平"
            : "强平仓位待确认"}{" "}
        · ${formatPrice(o.notional_usd)} · {o.source_venue ?? "场所未确认"} · 成交价{" "}
        {formatPrice(o.price)}
      </span>
    );
  if (o.market_kind === "smart_money" && o.parse_status === "parsed")
    return (
      <span className="news-market-title">
        {o.trader_label || o.account_address || "未标注账户"} ·{" "}
        {o.action === "open" ? "开仓" : o.action === "close" ? "平仓" : o.action} ·
        {o.position_side === "long" ? "多仓" : o.position_side === "short" ? "空仓" : "方向未记录"}{" "}
        · ${formatPrice(o.notional_usd)} · {o.source_venue}
      </span>
    );
  return <span className="news-market-title">{o.title}</span>;
}

export function OiEvidence({ observation: o }: { observation: NewsMarketObservation }) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  if (o.market_kind !== "oi") return null;
  return (
    <section className="news-oi-evidence" aria-label="OI 观察依据">
      <MarketObservationMetrics observation={o} />
      <p>
        这是供应商触发条件下保存的离散观察。OI
        上升或下降不能单独判断新增多头或空头；名义价值也受计价价格影响，不等于净资金流入。
      </p>
      <p>
        Whale Long Profit：{formatBps(o.whale_long_profit_bps)} · Whale/OI Ratio：
        {formatBps(o.whale_oi_ratio_bps)}
        。均为供应商口径；账户集合和计算分母未公开验证，不能视为鲸鱼胜率。
      </p>
      <div className="news-oi-evidence-actions">
        {o.measurement_contract_status === "proven" &&
        o.provider &&
        o.source_venue &&
        o.measurement_definition ? (
          <button
            className="news-market-kind-filter"
            type="button"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("kind", "oi");
              next.set("provider", o.provider!);
              next.set("venue", o.source_venue!);
              next.set("measurement_definition", o.measurement_definition!);
              next.set("sort", "oi_change");
              next.delete("item");
              navigate(`/news/market?${next}`);
            }}
          >
            按这条观察的口径比较
          </button>
        ) : null}
        <Link to={`/trading?tab=decisions&source_item_id=${o.item_id}`}>
          查看这条观察的策略判定
        </Link>
        <Link
          state={{ researchFrom: location.pathname + location.search }}
          to={`/news/market/${o.item_id}`}
        >
          打开完整观察
        </Link>
      </div>
    </section>
  );
}
