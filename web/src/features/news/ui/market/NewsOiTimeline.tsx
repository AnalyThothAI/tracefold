import type { CSSProperties } from "react";

import type { NewsMarketObservation } from "../../api/newsQueries";
import { clockTime } from "../../model/newsLabels";
import { formatBps } from "../../model/newsPrice";

/** Only like-for-like retained measurements form bars; every original remains in the timeline below. */
export function NewsOiTimeline({
  observations,
}: {
  observations: readonly NewsMarketObservation[];
}) {
  const reference = observations[0];
  if (
    !reference ||
    reference.market_kind !== "oi" ||
    reference.measurement_contract_status !== "proven" ||
    !reference.provider ||
    !reference.source_venue ||
    !reference.raw_instrument ||
    !reference.measurement_definition ||
    !reference.measurement_window_ms ||
    observations.some(
      (item) =>
        item.market_kind !== "oi" ||
        item.parse_status !== "parsed" ||
        item.measurement_contract_status !== "proven" ||
        item.provider !== reference.provider ||
        item.source_venue !== reference.source_venue ||
        item.symbol !== reference.symbol ||
        item.raw_instrument !== reference.raw_instrument ||
        item.measurement_definition !== reference.measurement_definition ||
        item.measurement_window_ms !== reference.measurement_window_ms ||
        item.oi_change_bps == null ||
        !Number.isFinite(item.oi_change_bps),
    )
  )
    return null;
  const samples = [...observations].sort((a, b) => a.event_at_ms - b.event_at_ms).slice(-8);
  const maximum = Math.max(1, ...samples.map((item) => Math.abs(item.oi_change_bps!)));
  return (
    <section className="news-oi-chart" aria-label="同口径离散 OI 观察">
      <h3>本组离散观察</h3>
      <div className="news-oi-bars">
        {samples.map((item) => (
          <div className="news-oi-bar" key={item.item_id}>
            <b>{formatBps(item.oi_change_bps)}</b>
            <span
              aria-hidden
              style={
                {
                  "--news-oi-height": `${(Math.abs(item.oi_change_bps!) / maximum) * 72}px`,
                } as CSSProperties
              }
            />
            <small>{clockTime(item.event_at_ms)}</small>
          </div>
        ))}
      </div>
      <p>最近 {samples.length} 条同口径记录；柱长表示变化幅度，正负见数值，不是连续 OI 行情。</p>
    </section>
  );
}
