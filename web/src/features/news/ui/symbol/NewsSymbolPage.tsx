import {
  TradingSymbolSection,
  tradingLedgerEntries,
  useTradingOrdersWithToken,
  type TradingOiLedgerEntry,
} from "@features/trading";
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
import { symbolPerspective } from "../../model/symbolPerspective";
import { NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";

import { NewsSymbolEvents } from "./NewsSymbolEvents";
import { NewsSymbolIdentity } from "./NewsSymbolIdentity";
import { NewsSymbolPerspective } from "./NewsSymbolPerspective";
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
  /* One batch for both capital sections: `交易视角` reads its newest case and `交易复盘` lists them all. */
  const tradingQuery = useTradingOrdersWithToken(token, normalized);
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

  /*
   * The capital lane's reading of this token's newest frame. Same query key as 交易复盘 below, so React
   * Query serves both from one poll, and the frame is matched by the `event_id` the ledger itself
   * published — never by symbol and time, which is the join the OI audit refuses to guess at.
   */
  const ledger = tradingLedgerEntries(tradingQuery.data);
  const newestCase = [...ledger.values()].sort(
    (a, b) => caseObservedAtMs(b) - caseObservedAtMs(a),
  )[0];
  /*
   * Only when the case named a frame. `event_id` is null by design for a case the deterministic OI trigger
   * did not author, and `=== ""` would have matched any row that carried an empty id rather than none.
   */
  const caseEventId = newestCase?.value.event_id ?? null;
  const perspective = symbolPerspective(
    caseEventId == null ? undefined : rows.find((row) => row.event_id === caseEventId),
    newestCase,
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

      {/* No count stamp here: 24H 事件 and 已推送 are two of the identity band's three tiles below, which
          is where the artifact puts them. Printing them in the header too showed a reader the same two
          numbers twice on one screen and made the band look like a restatement rather than the place. */}
      <NewsPageHeader subtitle="这个名字最近发生了什么，以及它到底是什么。" title={normalized} />

      {symbolQuery.isError && !symbolQuery.data ? (
        <PageState.Error error={symbolQuery.error} onRetry={() => void symbolQuery.refetch()} />
      ) : null}

      <div className="news-symbol-body">
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
            {
              key: "window",
              label: "OI 窗口",
              // Caution only when the window is full, which is the one state with a consequence: the
              // next qualifying frame for this name will be withheld by `beyond_window_rank`.
              tone: occupancy?.full ? "caution" : undefined,
              value: occupancy ? `${occupancy.used} / ${occupancy.max_rank_in_window}` : "—",
            },
          ]}
        />

        <NewsSymbolWindow
          occupancy={occupancy}
          oiPath={newsOiPath()}
          policy={statusQuery.data?.oi?.policy ?? null}
        />

        {/*
         * 交易视角 and 交易复盘 sit above the event list, as the artifact draws them (#282). The list is
         * the long tail; these two are the answer a reader arriving from a frame came for, and they were
         * both below a table that can run to a hundred rows.
         */}
        <NewsSymbolPerspective
          perspective={perspective}
          read={
            tradingQuery.isError && !tradingQuery.data
              ? "failed"
              : tradingQuery.data == null
                ? "loading"
                : "ready"
          }
        />

        <TradingSymbolSection base={normalized} token={token} />

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
    </NewsPageShell>
  );
}

/** A count the read has not answered yet is a dash, never a zero that means "still loading". */
function count(value: number | undefined): string {
  return value == null ? "—" : String(value);
}

/**
 * When the case observed its source fact. Both halves of the ledger batch answer it, under different names:
 * an order carries the case's own `case_observed_at_ms`, a case row carries `observed_at_ms`.
 */
function caseObservedAtMs(entry: TradingOiLedgerEntry): number {
  return entry.kind === "order"
    ? (entry.value.case_observed_at_ms ?? entry.value.created_at_ms)
    : entry.value.observed_at_ms;
}
