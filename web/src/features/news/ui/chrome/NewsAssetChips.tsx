import type { NewsAssetRef, NewsQuote } from "../../api/newsQueries";

import { NewsQuoteValue } from "./NewsQuoteValue";

import "./newsAssetChips.css";

/**
 * The assets an Event was grounded on, each shown as what it actually names (#87).
 *
 * A grounded tag reads `hl.perp:HYPE` — venue then ticker — so the reader can tell a Binance perp from a
 * Hyperliquid builder-DEX equity at a glance. A tag that resolves to nothing is struck through and outlined
 * instead: the provider tags `SPOT` on a Spot Gold headline and `NEAR` on the words "near-instant", and
 * before this there was nothing on screen separating those from a real listing.
 *
 * Whether a tag resolved is the server's answer, not this component's — it renders `listed` and never
 * consults a symbol table of its own.
 *
 * `quotes` is the current price for the same symbol, keyed by the tag that was requested (#88). It is an
 * independent poll on its own query key, so a price that moved does not invalidate the feed body; a chip with
 * no quote yet simply renders without one rather than holding the row back.
 */
export function NewsAssetChips({
  assets,
  label = "关联资产",
  quotes,
}: {
  assets: NewsAssetRef[];
  label?: string;
  quotes?: Record<string, NewsQuote>;
}) {
  if (!assets.length) return null;
  return (
    <span aria-label={label} className="news-asset-chips">
      {assets.map((asset) => (
        <code data-listed={asset.listed || undefined} key={asset.symbol}>
          {asset.venue ? <span className="news-asset-venue">{asset.venue}:</span> : null}
          <span
            className="news-asset-symbol"
            title={asset.listed ? undefined : "该符号未落在标的表上"}
          >
            {asset.symbol}
          </span>
          {quotes?.[asset.symbol] && asset.listed ? (
            <NewsQuoteValue quote={quotes[asset.symbol]} />
          ) : null}
        </code>
      ))}
    </span>
  );
}
