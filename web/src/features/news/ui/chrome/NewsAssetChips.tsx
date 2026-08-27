import { newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Link } from "react-router-dom";

import type { NewsAssetRef, NewsQuote } from "../../api/newsQueries";

import { NewsQuoteChange, NewsQuoteCompact } from "./NewsQuoteValue";

import "./newsAssetChips.css";

/**
 * The durable assets an Event concerns, each shown as what it actually names (#87/#287).
 *
 * A grounded tag reads `hl.perp:HYPE +2.6%` — venue, ticker, and what it did — with no frame around it. The
 * one framed thing in a meta line is a tag that resolved to nothing: the provider tags `SPOT` on a Spot Gold
 * headline and `NEAR` on the words "near-instant", so those are struck through inside a dashed amber box and
 * are impossible to mistake for a listing.
 *
 * Whether a tag resolved is the server's answer, not this component's — it renders `listed` and never
 * consults a symbol table of its own. `quotes` is an independent poll keyed by the requested tag (#88), so a
 * price that moved does not invalidate the feed body and a chip with no quote yet simply renders without one.
 *
 * Every chip is a link to the token page (#207 principle 9), including the struck-through ones: "the
 * provider tagged a name nothing lists" is a real answer and the endpoint gives it rather than a 404. The
 * link is on the symbol alone, not the whole chip, so a quote that ticks beside it is not part of the target
 * — and the row's own stretched headline link keeps working around it.
 */
export function NewsAssetChips({
  assets,
  label = "关联资产",
  max,
  quotes,
  withPrice = false,
}: {
  assets: NewsAssetRef[];
  label?: string;
  max?: number;
  quotes?: Record<string, NewsQuote>;
  withPrice?: boolean;
}) {
  const referrer = useRouteReferrer();
  if (!assets.length) return null;
  const shown = max == null ? assets : assets.slice(0, max);
  const overflow = assets.length - shown.length;
  return (
    <span aria-label={label} className="news-asset-chips">
      {shown.map((asset) => (
        <code data-listed={asset.listed || undefined} key={asset.symbol}>
          {asset.venue ? <span className="news-asset-venue">{asset.venue}:</span> : null}
          <Link
            className="news-asset-symbol"
            state={referrer}
            title={asset.listed ? `打开代币页 ${asset.base_symbol}` : "该符号未落在标的表上"}
            to={newsSymbolPath(asset.base_symbol)}
          >
            {asset.symbol}
          </Link>
          {asset.listed ? (
            withPrice ? (
              <NewsQuoteCompact quote={quotes?.[asset.symbol]} />
            ) : (
              <NewsQuoteChange quote={quotes?.[asset.symbol]} />
            )
          ) : null}
        </code>
      ))}
      {/* Three fit a row; the rest are counted and listed in full on the detail page. */}
      {overflow > 0 ? <span className="news-asset-more">+{overflow}</span> : null}
    </span>
  );
}
