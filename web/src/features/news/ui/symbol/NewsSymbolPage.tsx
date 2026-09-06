import { routeReferrerFromState } from "@shared/routing/routeReferrer";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { useEffect, useMemo, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import {
  NEWS_FEED_DEFAULT_HOURS,
  useNewsFeedHistoryWithToken,
  useNewsFeedWithToken,
  useNewsQuotesWithToken,
  useNewsSymbolWithToken,
  type NewsFeedFilters,
} from "../../api/newsQueries";
import { parseSymbolLane } from "../../model/symbolLanes";
import { NewsPageHeader } from "../chrome/NewsChrome";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";

import { NewsSymbolEvents } from "./NewsSymbolEvents";
import { NewsSymbolIdentity } from "./NewsSymbolIdentity";

import "./newsSymbol.css";

/**
 * 代币页 — what this name is, and everything that happened to it (#207 PR-W1).
 *
 * The console could answer "what did the pipeline decide about this Event" and "how is the lane doing", but
 * not "what has been going on with this token" — the question a reader actually arrives with after seeing a
 * headline. Every `base_symbol` on the console now routes here.
 *
 * Three reads, each already existing and each on its own key: identity from `/api/news/symbols/{base}`,
 * the Events from `/api/news/feed?symbol=`, and the current quote from `/api/news/quotes`. Nothing is
 * recomputed and nothing is merged into a field the browser owns. The fourth was this symbol's rank
 * occupancy out of `/api/news/status`; #458 removed the rank window with the push rule that spent it.
 *
 * Deliberately not built. There is no watchlist star because this read-only page owns no second watchlist
 * product or browser write. There is no price chart and no
 * open-interest curve — the same reason the OI monitor has none. The design's 交易视角 panel — the OI/price
 * quadrant and the pre-frame 1 h move — is still absent for the reason the OI monitor's is: both need the
 * price one hour before the frame, and the News price plane stores only the Event-anchored p0/p1/p4.
 * Alpha 复盘 is here, reading the Signal lane's own endpoint.
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
      assertionStatuses: [],
      changeStates: [],
      eventFamilies: [],
      eventKinds: [],
      finalDecisions: [],
      hours: NEWS_FEED_DEFAULT_HOURS,
      outcome: null,
      directions: [],
      q: "",
      sourceAuthorities: [],
      subjectCodes: [],
      symbol: normalized,
    }),
    [normalized],
  );
  const feedQuery = useNewsFeedWithToken(token, filters);
  /* One Case batch feeds both compact Alpha summaries on this symbol page. */
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

  return (
    <PageShell archetype="scan" className="news-symbol-shell" label={`代币 ${normalized}`}>
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

      {/* No count stamp here: 24H 事件 and 已推送 are two of the identity band's three tiles below, which
          is where the artifact puts them. Printing them in the header too showed a reader the same two
          numbers twice on one screen and made the band look like a restatement rather than the place. */}
      <NewsPageHeader subtitle="这个名字最近发生了什么，以及它到底是什么。" title={normalized} />

      {symbolQuery.isError && !symbolQuery.data ? (
        <PageState.Error error={symbolQuery.error} onRetry={() => void symbolQuery.refetch()} />
      ) : null}

      <div className="news-symbol-body">
        <NewsQuoteReadState query={quotesQuery}>
          <NewsSymbolIdentity
            quote={quotesQuery.data?.quotes?.[0]}
            symbol={symbolQuery.data}
            tiles={[
              { key: "events", label: "24H 事件", value: count(firstPage?.counts?.total) },
              {
                key: "pushed",
                label: "已推送",
                tone: "accent",
                value: count(firstPage?.counts?.pushed),
              },
            ]}
          />
        </NewsQuoteReadState>

        {/*
         * Alpha 复盘 sits above the event list. The list is the long
         * tail; this is the answer a reader arriving from a frame came for.
         *
         * The separate 交易视角 quadrant panel is gone with the quadrant itself (#331). It re-derived a
         * verdict from a Case's frozen `strategy_config` in the browser — a second interpretation of the
         * same numbers — and the OI/price quadrant it led with was `oi_momentum_v1`'s entry rule, which
         * no policy reads any more. The Case's own frozen checks are rendered where the Case lives.
         */}

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
      </div>
    </PageShell>
  );
}

/** A count the read has not answered yet is a dash, never a zero that means "still loading". */
function count(value: number | undefined): string {
  return value == null ? "—" : String(value);
}
