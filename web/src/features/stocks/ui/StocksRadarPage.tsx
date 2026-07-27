import {
  compactNumber,
  formatRelativeTime,
  formatRisk,
  formatSignedPercent,
  formatTokenPriceUsd,
} from "@lib/format";
import type { StockRadarRow, WindowKey } from "@lib/types";
import * as PageState from "@shared/ui/PageState";
import { RadarControls } from "@shared/ui/RadarControls";
import clsx from "clsx";
import { AlertTriangle, ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

import { useStocksRadarQuery } from "../api/useStocksRadarQuery";
import "./stocks.css";

type StocksRadarPageProps = {
  token: string;
  windowKey: WindowKey;
  onWindowChange: (window: WindowKey) => void;
};

export function StocksRadarPage({ token, windowKey, onWindowChange }: StocksRadarPageProps) {
  const query = useStocksRadarQuery({ token, window: windowKey });
  const data = query.data;
  const rows = data?.rows ?? [];
  return (
    <section aria-label="US stocks radar" className="stocks-radar-panel" data-page-archetype="scan">
      <header className="stocks-radar-toolbar">
        <div>
          <h2>US Stocks</h2>
          <span>
            STOCK RADAR <b>{compactNumber(rows.length)}</b>
          </span>
        </div>
        <div className="stocks-health" aria-label="stocks radar health">
          <span>{windowKey}</span>
          <span>
            quotes <b>{compactNumber(data?.health.quote_ready_count ?? 0)}</b>
          </span>
          <span>
            stale <b>{compactNumber(data?.health.quote_unavailable_count ?? 0)}</b>
          </span>
        </div>
        <div className="stocks-radar-controls" aria-label="stocks radar controls">
          <RadarControls windowKey={windowKey} onWindowChange={onWindowChange} />
        </div>
      </header>

      <div className="stocks-radar-table">
        <div className="stock-radar-head">
          <span>Symbol</span>
          <span>Mentions</span>
          <span>Authors</span>
          <span>Latest</span>
          <span>Price</span>
          <span>Move</span>
          <span>Quote</span>
        </div>
        {query.isLoading ? <PageState.TableSkeleton rows={8} label="loading stocks radar" /> : null}
        {!query.isLoading && rows.length === 0 ? (
          query.isError ? (
            <PageState.Error error={query.error ?? "Stocks radar unavailable"} />
          ) : (
            <PageState.Empty title="No stock flow" />
          )
        ) : null}
        {rows.map((row) => (
          <StockRow key={row.target.target_id} row={row} />
        ))}
      </div>
    </section>
  );
}

function StockRow({ row }: { row: StockRadarRow }) {
  const change = row.quote.change_pct;
  const direction =
    change === null || change === undefined
      ? "flat"
      : change > 0
        ? "up"
        : change < 0
          ? "down"
          : "flat";
  const quoteReady = row.quote.status === "ready";
  const quoteLabel = quoteReady ? row.quote.provider || "ready" : formatRisk(row.quote.error);
  return (
    <article className="stock-radar-row" aria-label={`stock ${row.target.symbol}`}>
      <span className="stock-token-cell">
        <strong className="stock-token-symbol">
          <span className="stock-symbol-line">
            <span>${row.target.symbol}</span>
          </span>
          <small>
            {[row.target.exchange, row.target.name].filter(Boolean).join(" · ") || "US equity"}
          </small>
        </strong>
      </span>

      <span className="stock-mentions-cell" data-radar-metric="heat">
        <b className={row.attention.mentions > 0 ? "stock-score-hot" : "stock-score-warn"}>
          {compactNumber(row.attention.mentions)}
        </b>
        <small>all source posts</small>
      </span>

      <span className="stock-authors-cell" data-radar-metric="quality">
        <b>{compactNumber(row.attention.unique_authors)}</b>
        <small>unique authors</small>
      </span>

      <span className="stock-latest-cell" data-radar-metric="propagation">
        <b>{row.latest_event.author_handle ? `@${row.latest_event.author_handle}` : "-"}</b>
        <small>{formatRelativeTime(row.latest_event.received_at_ms)} ago</small>
      </span>

      <span className="stock-price-cell" data-radar-metric="market">
        <b>{formatTokenPriceUsd(row.quote.price)}</b>
        <small>{row.quote.provider_symbol || row.target.symbol || "US equity"}</small>
      </span>

      <span className="stock-move-cell" data-radar-metric="timing">
        <span className="stock-move-line">
          {direction === "up" ? <ArrowUpRight aria-hidden /> : null}
          {direction === "down" ? <ArrowDownRight aria-hidden /> : null}
          {direction === "flat" ? <Minus aria-hidden /> : null}
          <b className={clsx("stock-direction", direction)}>{formatSignedPercent(change)}</b>
        </span>
        <small>{moveMeta(row)}</small>
      </span>

      <span className={clsx("stock-quote-cell", quoteReady ? "ready" : "unavailable")}>
        {quoteReady ? null : <AlertTriangle aria-hidden />}
        <b>{quoteLabel}</b>
        <small>{row.quote.latency_class || row.quote.freshness_class || row.quote.status}</small>
      </span>
    </article>
  );
}

function moveMeta(row: StockRadarRow): string {
  if (row.quote.reference_close_price !== null && row.quote.reference_close_price !== undefined) {
    return `prev ${formatTokenPriceUsd(row.quote.reference_close_price)}`;
  }
  if (row.row_health.length > 0) {
    return formatRisk(row.row_health[0]);
  }
  return "quote pending";
}
