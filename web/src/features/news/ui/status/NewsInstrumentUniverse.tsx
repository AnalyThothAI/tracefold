import { Metric, MetricRow } from "@shared/ui/Metric";

import type { NewsStatus } from "../../api/newsQueries";
import { formatCount, optionalTime } from "../../model/newsLabels";
import { NewsEmptyNote } from "../chrome/NewsChrome";

import "./newsInstrumentUniverse.css";

/**
 * What the venue catalogues hold (#75, surfaced by #87). Not a fifth health card: the per-venue snapshot
 * failures live in the worker process and are never persisted, so there is no *venue* health to render — the
 * table below the figures is inventory, not status.
 *
 * `参考目录` counts the US listed-symbol tier (#91) — tickers that tell the Gate a headline is about a stock and
 * that nobody can trade here, which is why every other figure and both breakdowns exclude them.
 *
 * `dangling_aliases` is the one number here the server does state a target for — a seed alias pointing at a
 * symbol no venue lists resolves to nothing, silently, which is how `1810.HK -> XIAOMI` went unnoticed for a
 * week (#89). It should be 0, so it takes the caution tone the moment it is not. Rendering the whole summary
 * also means a field the server adds cannot go unnoticed the way these two did.
 */
export function InstrumentUniverse({ status }: { status: NewsStatus }) {
  const universe = status.instruments;
  if (!universe || !universe.last_snapshot_ms) {
    return <NewsEmptyNote>还没有快照落地，符号归一暂时只走别名种子。</NewsEmptyNote>;
  }
  const byVenue = Object.entries(universe.by_venue ?? {});
  const byClass = Object.entries(universe.by_class ?? {});
  const dangling = universe.dangling_aliases ?? 0;
  return (
    <div className="news-universe">
      <MetricRow columns={5} label="标的表摘要">
        <Metric
          caption="在交易合约"
          eyebrow="TRADING"
          note={`已下架 ${formatCount(universe.delisted ?? 0)}`}
          size="sm"
          value={formatCount(universe.trading ?? 0)}
        />
        <Metric
          caption="base 符号"
          eyebrow="SYMBOLS"
          note={`场所 ${formatCount(universe.venues ?? 0)}`}
          size="sm"
          value={formatCount(universe.base_symbols ?? 0)}
        />
        <Metric
          caption="参考目录"
          eyebrow="US LISTED"
          size="sm"
          title="美股上市代码，只用来判断标的是不是股票，在这里不可交易"
          value={formatCount(universe.reference_symbols ?? 0)}
        />
        <Metric
          caption="最近快照"
          eyebrow="SNAPSHOT"
          size="sm"
          value={optionalTime(universe.last_snapshot_ms).slice(11) || "尚无"}
        />
        <Metric
          caption="悬空别名"
          eyebrow="DANGLING"
          note="目标 0"
          size="sm"
          title={
            dangling
              ? "别名指向的符号在任何场所都没有挂牌，会静默解析不到"
              : "每个种子别名都指向一个真实挂牌的符号"
          }
          tone={dangling ? "caution" : "plain"}
          value={formatCount(dangling)}
        />
      </MetricRow>

      <section aria-label="按场所" className="news-universe-table">
        <div className="news-universe-head">
          <span>VENUE</span>
          <span>符号</span>
        </div>
        {byVenue.map(([venue, count]) => (
          <div className="news-universe-row" key={venue}>
            <code>{venue}</code>
            <b>{formatCount(count)}</b>
          </div>
        ))}
      </section>

      {byClass.length ? (
        <ul aria-label="按资产类别" className="news-universe-classes">
          {byClass.map(([cls, count]) => (
            <li key={cls}>
              <code>{cls}</code>
              <b>{formatCount(count)}</b>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
