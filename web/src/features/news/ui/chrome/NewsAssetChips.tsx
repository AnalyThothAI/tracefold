import type { NewsAssetRef, NewsQuote } from "../../api/newsQueries";

import { NewsQuoteChange } from "./NewsQuoteValue";

import "./newsAssetChips.css";

/**
 * The assets an Event was grounded on, each shown as what it actually names (#87).
 *
 * A grounded tag reads `hl.perp:HYPE +2.6%` — venue, ticker, and what it did — with no frame around it. The
 * one framed thing in a meta line is a tag that resolved to nothing: the provider tags `SPOT` on a Spot Gold
 * headline and `NEAR` on the words "near-instant", so those are struck through inside a dashed amber box and
 * are impossible to mistake for a listing.
 *
 * Whether a tag resolved is the server's answer, not this component's — it renders `listed` and never
 * consults a symbol table of its own. `quotes` is an independent poll keyed by the requested tag (#88), so a
 * price that moved does not invalidate the feed body and a chip with no quote yet simply renders without one.
 */
export function NewsAssetChips({
  assets,
  label = "关联资产",
  max,
  quotes,
}: {
  assets: NewsAssetRef[];
  label?: string;
  max?: number;
  quotes?: Record<string, NewsQuote>;
}) {
  if (!assets.length) return null;
  const shown = max == null ? assets : assets.slice(0, max);
  const overflow = assets.length - shown.length;
  return (
    <span aria-label={label} className="news-asset-chips">
      {shown.map((asset) => (
        <code data-listed={asset.listed || undefined} key={asset.symbol}>
          {asset.venue ? <span className="news-asset-venue">{asset.venue}:</span> : null}
          <span
            className="news-asset-symbol"
            title={asset.listed ? undefined : "该符号未落在标的表上"}
          >
            {asset.symbol}
          </span>
          {asset.listed ? <NewsQuoteChange quote={quotes?.[asset.symbol]} /> : null}
        </code>
      ))}
      {/* Three fit a row; the rest are counted and listed in full on the detail page. */}
      {overflow > 0 ? <span className="news-asset-more">+{overflow}</span> : null}
    </span>
  );
}
