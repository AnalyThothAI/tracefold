import type { CSSProperties } from "react";

import type { TradingExecutionReadiness } from "../api/tradingQueries";

type Position = NonNullable<
  NonNullable<TradingExecutionReadiness["current_account"]>["positions"]
>[number];

/** Price geometry only. Original decimal strings remain the displayed facts. */
export function TradingPriceRange({ position, stale }: { position: Position; stale: boolean }) {
  const stop = Number(position.stop_trigger_price);
  const target = Number(position.take_profit_trigger_price);
  const entry = Number(position.entry_price);
  const mark = Number(position.mark_price);
  if (
    position.stop_trigger_price == null ||
    position.take_profit_trigger_price == null ||
    position.mark_price == null ||
    ![stop, target, entry, mark].every((value) => Number.isFinite(value) && value > 0) ||
    stop === target
  )
    return null;
  const low = Math.min(stop, target);
  const high = Math.max(stop, target);
  const percentage = (value: number) =>
    Math.max(0, Math.min(100, ((value - low) / (high - low)) * 100));
  const outside = mark < low || mark > high;
  const limits =
    stop < target
      ? [
          ["止损触发价", position.stop_trigger_price],
          ["止盈触发价", position.take_profit_trigger_price],
        ]
      : [
          ["止盈触发价", position.take_profit_trigger_price],
          ["止损触发价", position.stop_trigger_price],
        ];
  return (
    <div className="trading-price-range" aria-label="已记录的退出价格区间">
      <div className="trading-price-range-caption">
        <span>退出价格区间</span>
        <span>
          {stale ? "上次标记" : "标记"} {position.mark_price}
        </span>
      </div>
      <div
        className="trading-price-track"
        aria-hidden
        style={
          {
            "--trading-mark-offset": `${percentage(mark)}%`,
            "--trading-entry-offset": `${percentage(entry)}%`,
          } as CSSProperties
        }
      >
        <span className="trading-price-entry" />
        <span className="trading-price-mark" data-outside={outside || undefined} />
      </div>
      <div className="trading-price-limits">
        {limits.map(([label, value]) => (
          <span key={label}>
            <b>{value}</b>
            <small>{label}</small>
          </span>
        ))}
      </div>
      {outside ? <p>标记价已超出已记录的退出区间；执行结果以交易所记录为准。</p> : null}
    </div>
  );
}
