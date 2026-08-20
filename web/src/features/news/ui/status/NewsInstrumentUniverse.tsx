import type { NewsStatus } from "../../api/newsQueries";
import { formatCount, optionalTime } from "../../model/newsLabels";
import { NewsEmptyNote } from "../chrome/NewsChrome";

import "./newsInstrumentUniverse.css";

/**
 * What the venue catalogues hold (#75, surfaced by #87). Not a fifth health card: the per-venue snapshot
 * failures live in the worker process and are never persisted, so there is no *venue* health to render.
 *
 * `参考目录` counts the US listed-symbol tier (#91) — tickers that tell the Gate a headline is about a stock and
 * that nobody can trade here, which is why every other figure and both breakdowns exclude them.
 *
 * `dangling_aliases` is the one number here the server does state a target for — a seed alias pointing at a
 * symbol no venue lists resolves to nothing, silently, which is how `1810.HK -> XIAOMI` went unnoticed for a
 * week (#89). It should be 0, so it is toned the moment it is not; the rest are inventory. Rendering the
 * whole summary also means a field the server adds cannot go unnoticed the way these two did.
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
      <dl className="news-universe-figures">
        <div>
          <dt>在交易合约</dt>
          <dd>{formatCount(universe.trading ?? 0)}</dd>
        </div>
        <div>
          <dt>已下架</dt>
          <dd>{formatCount(universe.delisted ?? 0)}</dd>
        </div>
        <div>
          <dt>base 符号</dt>
          <dd>{formatCount(universe.base_symbols ?? 0)}</dd>
        </div>
        <div>
          <dt>场所</dt>
          <dd>{formatCount(universe.venues ?? 0)}</dd>
        </div>
        <div>
          <dt>参考目录</dt>
          <dd title="美股上市代码，只用来判断标的是不是股票，在这里不可交易">
            {formatCount(universe.reference_symbols ?? 0)}
          </dd>
        </div>
        <div>
          <dt>最近快照</dt>
          <dd>{optionalTime(universe.last_snapshot_ms)}</dd>
        </div>
        <div className="news-toned" data-tone={dangling ? "caution" : undefined}>
          <dt>悬空别名</dt>
          <dd title={dangling ? "别名指向的符号在任何场所都没有挂牌，会静默解析不到" : undefined}>
            {formatCount(dangling)}
          </dd>
        </div>
      </dl>
      {byClass.length ? (
        <ul aria-label="按资产类别" className="news-universe-venues">
          {byClass.map(([cls, count]) => (
            <li key={cls}>
              <code>{cls}</code>
              <b>{formatCount(count)}</b>
            </li>
          ))}
        </ul>
      ) : null}
      <ul aria-label="按场所" className="news-universe-venues">
        {byVenue.map(([venue, count]) => (
          <li key={venue}>
            <code>{venue}</code>
            <b>{formatCount(count)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}
