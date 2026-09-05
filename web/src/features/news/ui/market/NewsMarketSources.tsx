import {
  NEWS_MARKET_KINDS,
  type NewsMarketKind,
  type NewsMarketSource,
} from "../../api/newsQueries";
import { marketKindLabel, marketKindTitle } from "../../model/marketFacts";
import { formatCount, optionalTime } from "../../model/newsLabels";

import "./newsMarketSources.css";

/**
 * What each source sent in this window, counted off the stored facts (#553 PR-1).
 *
 * Two rows of figures, and they answer two different questions (#553). The first is intake: `received` is
 * how many records arrived, `parsed` and `raw` split them by whether a parser could read fields out of one,
 * and `groups` is how many rows the table below collapses them into. `raw` is not a failure count — an
 * `unknown_market` source has no parser at all and is `raw` by definition — which is why it sits beside
 * `parsed` rather than under it.
 *
 * The second is what a reader was actually told: how many observations a card spoke for without triggering
 * one, and how many cards were sent, failed, or came back with no answer this process could read. `unknown`
 * is its own figure and is never folded into `failed`: the provider may well have delivered those.
 *
 * These are fact and receipt counts from the same two reads the list already makes. There is no gate
 * dashboard behind them and no second endpoint.
 *
 * A kind the server did not report a summary for is drawn with zeroes rather than hidden: "this source sent
 * nothing in the last 72 hours" is the answer a reader came for.
 */
export function NewsMarketSources({
  selected,
  sources,
}: {
  selected: readonly NewsMarketKind[];
  sources: readonly NewsMarketSource[];
}) {
  const byKind = new Map(sources.map((source) => [source.market_kind, source]));
  return (
    <div aria-label="来源汇总" className="news-market-sources">
      {NEWS_MARKET_KINDS.map((kind) => (
        <SourceTile
          key={kind}
          kind={kind}
          muted={selected.length > 0 && !selected.includes(kind)}
          source={byKind.get(kind)}
        />
      ))}
    </div>
  );
}

function SourceTile({
  kind,
  muted,
  source,
}: {
  kind: NewsMarketKind;
  muted: boolean;
  source: NewsMarketSource | undefined;
}) {
  return (
    <div
      className="news-market-source"
      data-muted={muted || undefined}
      title={marketKindTitle(kind)}
    >
      <span className="news-market-source-head">
        <b>{marketKindLabel(kind)}</b>
        <small>{optionalTime(source?.last_received_at_ms)}</small>
      </span>
      <span className="news-market-source-figures">
        <SourceFigure label="收到" value={source?.received} />
        <SourceFigure label="已解析" value={source?.parsed} />
        <SourceFigure label="仅原文" value={source?.raw} />
        <SourceFigure label="组" value={source?.groups} />
      </span>
      <span className="news-market-source-figures" data-row="delivery">
        <SourceFigure label="合并" value={source?.merged} />
        <SourceFigure label="已发" value={source?.sent} />
        <SourceFigure label="失败" value={source?.failed} />
        <SourceFigure label="结果不明" value={source?.unknown} />
      </span>
    </div>
  );
}

function SourceFigure({ label, value }: { label: string; value: number | undefined }) {
  return (
    <span className="news-market-source-figure">
      <small>{label}</small>
      <b>{formatCount(value ?? 0)}</b>
    </span>
  );
}
