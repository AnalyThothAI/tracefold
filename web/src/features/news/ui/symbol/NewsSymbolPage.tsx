import { TradingSymbolSection } from "@features/trading";
import { newsOiPath } from "@shared/routing/paths";
import { routeReferrerFromState } from "@shared/routing/routeReferrer";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { useEffect, useMemo, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import {
  NEWS_FEED_DEFAULT_HOURS,
  useNewsFeedHistoryWithToken,
  useNewsFeedWithToken,
  useNewsQuotesWithToken,
  useNewsStatusWithToken,
  useNewsSymbolWithToken,
  type NewsFeedFilters,
} from "../../api/newsQueries";
import { parseSymbolLane } from "../../model/symbolLanes";
import { NewsPageHeader, NewsPageShell, NewsPageStamp } from "../chrome/NewsChrome";

import { NewsSymbolEvents } from "./NewsSymbolEvents";
import { NewsSymbolIdentity } from "./NewsSymbolIdentity";
import { NewsSymbolWindow } from "./NewsSymbolWindow";

import "./newsSymbol.css";

/**
 * 代币页 — what this name is, and everything that happened to it (#207 PR-W1).
 *
 * The console could answer "what did the pipeline decide about this Event" and "how is the lane doing", but
 * not "what has been going on with this token" — the question a reader actually arrives with after seeing a
 * headline. Every `base_symbol` on the console now routes here.
 *
 * Four reads, each already existing and each on its own key: identity from `/api/news/symbols/{base}`, the
 * Events from `/api/news/feed?symbol=`, the current quote from `/api/news/quotes`, and this symbol's rank
 * occupancy out of the OI section of `/api/news/status`. Nothing is recomputed and nothing is merged into a
 * field the browser owns.
 *
 * Deliberately not built. There is no watchlist star: `watchlistHardCut.test.ts` keeps that vocabulary out
 * of production source, and it would be a third browser write besides. There is no price chart and no
 * open-interest curve — the same reason the OI monitor has none. The design's 交易视角 panel — the OI/price
 * quadrant and the pre-frame 1 h move — is still absent for the reason the OI monitor's is: both need the
 * price one hour before the frame, and the News price plane stores only the Event-anchored p0/p1/p4.
 * 交易复盘 is here, reading the capital lane's own endpoint.
 */
export function NewsSymbolPage({ base, token }: { base: string; token: string }) {
  const normalized = base.trim().toUpperCase().replace(/^XYZ-/, "");
  const referrer = routeReferrerFromState(useLocation().state);
  const [searchParams, setSearchParams] = useSearchParams();
  const lane = parseSymbolLane(searchParams.get("lane"));

  const symbolQuery = useNewsSymbolWithToken(token, normalized);
  const filters = useMemo<NewsFeedFilters>(
    () => ({
      admission: null,
      decision: null,
      family: null,
      hours: NEWS_FEED_DEFAULT_HOURS,
      outcome: null,
      directions: [],
      channels: [],
      q: "",
      symbol: normalized,
    }),
    [normalized],
  );
  const feedQuery = useNewsFeedWithToken(token, filters);
  const statusQuery = useNewsStatusWithToken(token);
  const quotesQuery = useNewsQuotesWithToken(token, normalized ? [normalized] : []);

  /*
   * The history anchor is captured once and then held, exactly as the feed and the OI monitor hold theirs:
   * feeding the polled first page's `next_cursor` straight in would move it whenever an Event lands, change
   * the infinite query's key, and silently discard every page the reader had loaded.
   */
  const [moreRequested, setMoreRequested] = useState(false);
  const [anchor, setAnchor] = useState<{ base: string; cursor: string | null } | null>(null);
  const firstPage = feedQuery.data;
  useEffect(() => {
    if (!firstPage || anchor?.base === normalized) return;
    setAnchor({ base: normalized, cursor: firstPage.next_cursor ?? null });
    setMoreRequested(false);
  }, [anchor?.base, firstPage, normalized]);
  const anchorCursor = anchor?.base === normalized ? anchor.cursor : null;
  const historyQuery = useNewsFeedHistoryWithToken(token, filters, anchorCursor, moreRequested);
  const rows = Array.from(
    new Map(
      [firstPage?.events ?? [], ...(historyQuery.data?.pages ?? []).map((page) => page.events)]
        .flat()
        .map((event) => [event.event_id, event]),
    ).values(),
  );

  const occupancy = (statusQuery.data?.oi?.window_occupancy ?? []).find(
    (row) => row.symbol === normalized,
  );

  return (
    <NewsPageShell archetype="scan" className="news-symbol-shell" label={`代币 ${normalized}`}>
      {/*
       * The way back (#256). The artifact draws it in the frame; it lives on the page here because what
       * makes it correct — which page the reader left, and the filters they left it with — is route state,
       * and the frame does not hold it. Four surfaces link here, so a link that always said 事件流 named a
       * page the reader had never been on three times out of four.
       */}
      <header className="news-symbol-toolbar">
        <RouteBackLink
          ariaLabel={`返回${referrer.label}`}
          label={referrer.label}
          to={referrer.to}
        />
      </header>
      <NewsPageHeader subtitle="这个名字最近发生了什么，以及它到底是什么。" title={normalized}>
        {firstPage?.counts ? (
          <NewsPageStamp>
            24h {firstPage.counts.total} 条 · 已推送 {firstPage.counts.pushed}
          </NewsPageStamp>
        ) : null}
      </NewsPageHeader>

      {symbolQuery.isError && !symbolQuery.data ? (
        <PageState.Error error={symbolQuery.error} onRetry={() => void symbolQuery.refetch()} />
      ) : null}

      <div className="news-symbol-body">
        <NewsSymbolIdentity quote={quotesQuery.data?.quotes?.[0]} symbol={symbolQuery.data} />

        <NewsSymbolWindow
          occupancy={occupancy}
          oiPath={newsOiPath()}
          policy={statusQuery.data?.oi?.policy ?? null}
        />

        <NewsSymbolEvents
          error={feedQuery.isError && !feedQuery.data ? feedQuery.error : null}
          hasMore={Boolean(moreRequested ? historyQuery.hasNextPage : anchorCursor)}
          lane={lane}
          loading={feedQuery.isLoading}
          loadingMore={historyQuery.isFetchingNextPage || (moreRequested && historyQuery.isLoading)}
          onLaneChange={(next) => {
            const params = new URLSearchParams(searchParams);
            if (next === "all") params.delete("lane");
            else params.set("lane", next);
            setSearchParams(params, { replace: true });
          }}
          onLoadMore={() => {
            if (!moreRequested) setMoreRequested(true);
            else void historyQuery.fetchNextPage();
          }}
          onRetry={() => void feedQuery.refetch()}
          rows={rows}
        />

        {/*
         * The capital lane's own account of this token (#207 PR-W4). Owned by `features/trading` because
         * every word in it — case state, order state, the rule a case stopped on — is that lane's
         * vocabulary, and a copy of it here would be a second place those words could drift.
         */}
        <TradingSymbolSection base={normalized} token={token} />
      </div>
    </NewsPageShell>
  );
}
