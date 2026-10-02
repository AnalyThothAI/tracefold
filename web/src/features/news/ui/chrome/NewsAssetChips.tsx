import { canOpenNewsSymbol, newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Link } from "react-router-dom";

import { newsAssetKey, type NewsAssetRef, type NewsQuote } from "../../api/newsQueries";

import { NewsQuoteChange, NewsQuoteCompact } from "./NewsQuoteValue";

import "./newsAssetChips.css";

/**
 * The API owns asset selection and catalogue resolution. Only an explicitly unlisted typed asset is
 * marked as missing; reference-only and unresolved-market assets remain ordinary chips. Quotes poll
 * independently under the complete market/symbol key, and missing prices never hide an asset.
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
        <code
          data-resolution={asset.resolution_state}
          key={newsAssetKey(asset.market_type, asset.symbol)}
        >
          {asset.market_type !== "unknown" ? (
            <span className="news-asset-venue">{asset.market_type} · </span>
          ) : null}
          {asset.venue ? <span className="news-asset-venue">{asset.venue}:</span> : null}
          {canOpenNewsSymbol(asset.base_symbol) ? (
            <Link
              className="news-asset-symbol"
              state={referrer}
              title={
                asset.resolution_state === "unresolved_market"
                  ? "市场未确定，暂不报价"
                  : `打开标的页 ${asset.base_symbol}`
              }
              to={newsSymbolPath(asset.base_symbol)}
            >
              {asset.symbol}
            </Link>
          ) : (
            <span className="news-asset-symbol" title="未匹配行情标的">
              {asset.symbol}
            </span>
          )}
          {asset.resolution_state === "unlisted" ? (
            <span className="news-asset-unlisted"> · 交易所未上架</span>
          ) : null}
          {asset.market_type !== "unknown" ? (
            withPrice ? (
              <NewsQuoteCompact quote={quotes?.[newsAssetKey(asset.market_type, asset.symbol)]} />
            ) : (
              <NewsQuoteChange quote={quotes?.[newsAssetKey(asset.market_type, asset.symbol)]} />
            )
          ) : (
            <span className="news-asset-venue"> · 市场未定</span>
          )}
        </code>
      ))}
      {/* Three fit a row; the rest are counted and listed in full on the detail page. */}
      {overflow > 0 ? <span className="news-asset-more">+{overflow}</span> : null}
    </span>
  );
}
