import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { newsFeedIdentity } from "@shared/query/queryKeys";
import { ActionButton } from "@shared/ui/ActionButton";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  type NewsFeedFilters,
  type NewsFeedSearch,
  useNewsFeedHistoryWithToken,
  useNewsFeedWithToken,
  useNewsQuotesWithToken,
  useNewsStatusWithToken,
} from "../../api/newsQueries";
import {
  hasAdvancedFilters,
  nextFeedParams,
  parseFeedFilters,
  type FeedFilterChanges,
} from "../../model/feedFilters";
import { newsFeedGroups } from "../../model/feedGroups";
import { absoluteTime, formatCount, hoursLabel, outcomeTabLabel } from "../../model/newsLabels";
import { useAnchoredEventFeed } from "../../state/useAnchoredEventFeed";
import { NewsPageHeader, NewsPageStamp } from "../chrome/NewsChrome";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";
import { NewsEventDrawer } from "../detail/NewsEventDrawer";

import { NewsEventRow } from "./NewsEventRow";
import { NewsFeedToolbar } from "./NewsFeedToolbar";
import { NewsFunnelCard } from "./NewsFunnelCard";

import "./newsFeed.css";

/**
 * Where the Event drawer earns its place: wide enough that a 420px sheet still leaves the list readable
 * beside it. Below this, opening an Event is a page.
 */
const DRAWER_QUERY = "(min-width: 1024px)";

/**
 * The decision-first scan surface over the flat Event feed. The browser never clusters, scores, triages,
 * throttles or reorders — it asks the server for a page and renders one row per Event.
 *
 * Live inserts are held back so polling never shifts the row under the pointer. On a wide screen, opening an
 * Event uses the existing drawer so the list stays visible; every row remains a real detail link.
 */
export function NewsFeedPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = parseFeedFilters(searchParams);
  const query = useNewsFeedWithToken(token, filters);
  const statusQuery = useNewsStatusWithToken(token);
  const feedIdentity = newsFeedIdentity(filters).join("");
  const [historyAnchor, setHistoryAnchor] = useState<{
    cursor: string | null;
    feedIdentity: string;
  } | null>(null);
  const [historyRequested, setHistoryRequested] = useState(false);
  const firstPage = query.data;
  useEffect(() => {
    if (!firstPage || historyAnchor?.feedIdentity === feedIdentity) return;
    setHistoryAnchor({ cursor: firstPage.next_cursor ?? null, feedIdentity });
    setHistoryRequested(false);
  }, [feedIdentity, firstPage, historyAnchor?.feedIdentity]);
  const historyCursor = historyAnchor?.feedIdentity === feedIdentity ? historyAnchor.cursor : null;
  const historyQuery = useNewsFeedHistoryWithToken(token, filters, historyCursor, historyRequested);
  const pages = historyQuery.data?.pages ?? [];
  const serverEvents = Array.from(
    new Map(
      [firstPage?.events ?? [], ...pages.map((page) => page.events)]
        .flat()
        .map((event) => [event.event_id, event]),
    ).values(),
  );
  const hasAdvanced = hasAdvancedFilters(filters);
  const eventListRef = useRef<HTMLDivElement>(null);
  const eventFeed = useAnchoredEventFeed(
    eventListRef,
    serverEvents,
    firstPage?.events ?? [],
    feedIdentity,
  );
  const events = eventFeed.events;
  // One quote request for everything on screen (#88): the symbols the server already resolved, deduplicated
  // into a single query key. Prices never travel in the feed body — that would make its ETag useless.
  const quotesQuery = useNewsQuotesWithToken(
    token,
    events.flatMap((event) => (event.assets ?? []).filter((a) => a.listed).map((a) => a.symbol)),
  );
  const quotes = Object.fromEntries(
    (quotesQuery.data?.quotes ?? []).map((quote) => [quote.requested_symbol, quote]),
  );
  const feedSearch = nextFeedParams(filters, {}).toString();
  const wideEnoughForDrawer = useMediaQuery(DRAWER_QUERY);
  const [drawerId, setDrawerId] = useState<string | null>(null);
  const drawerTriggerRef = useRef<HTMLAnchorElement | null>(null);

  const updateFeedParams = (changes: FeedFilterChanges) => {
    setSearchParams(nextFeedParams(filters, changes), { replace: true });
  };

  return (
    <PageShell archetype="scan" className="news-feed-shell" label="新闻事件流">
      {/*
       * The pipeline health pill used to sit here. It is the topbar lamp now (#207), present after every
       * successful health read because it is also the status-page door. Two health controls on one screen
       * saying the same thing is one of them the reader learns to skip.
       */}
      <NewsPageHeader subtitle="每条新闻的判定与去向；符号可点进代币页" title="新闻事件流">
        {statusQuery.data ? (
          <NewsPageStamp>
            更新于 {absoluteTime(statusQuery.data.measured_at_ms).slice(11)}
          </NewsPageStamp>
        ) : null}
      </NewsPageHeader>

      {statusQuery.data ? (
        <NewsFunnelCard status={statusQuery.data} />
      ) : statusQuery.isLoading ? (
        <PageState.TileSkeleton label="正在读取 24 小时漏斗" />
      ) : null}

      <NewsFeedToolbar
        counts={firstPage?.counts ?? undefined}
        filters={filters}
        onChange={updateFeedParams}
        visibleCount={events.length}
      />

      {firstPage?.search ? <NewsSearchScope filters={filters} search={firstPage.search} /> : null}

      {query.isLoading && !query.data ? (
        <PageState.Loading label="正在读取新闻事件" layout="panel" rows={6} />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {!query.isLoading && !query.isError && !events.length ? (
        <PageState.Empty
          action={
            hasAdvanced || filters.outcome !== null || filters.hours !== 24 ? (
              <ActionButton
                onClick={() =>
                  updateFeedParams({
                    admission: null,
                    assertionStatuses: [],
                    changeStates: [],
                    directions: [],
                    eventFamilies: [],
                    eventKinds: [],
                    finalDecisions: [],
                    hours: 24,
                    outcome: null,
                    q: null,
                    sourceAuthorities: [],
                    subjectCodes: [],
                    symbol: null,
                  })
                }
              >
                清除搜索与筛选
              </ActionButton>
            ) : null
          }
          hint={emptyHint(filters, firstPage?.search ?? null)}
          title={emptyTitle(filters, firstPage?.search ?? null)}
        />
      ) : null}
      {events.length ? (
        <PageState.Stale
          className="news-feed-page-state"
          updating={query.isFetching && !query.isLoading}
        >
          <NewsQuoteReadState query={quotesQuery}>
            <div className="news-feed-results">
              {eventFeed.count ? (
                <button className="news-new-events" onClick={eventFeed.reveal} type="button">
                  <span aria-hidden className="news-new-events-dot" />
                  {eventFeed.count} 条新事件 · 回到顶部
                </button>
              ) : null}
              <div className="news-event-list" ref={eventListRef}>
                <div aria-hidden className="news-event-list-header">
                  <span>TIME</span>
                  <span>EVENT</span>
                  <span>OUTCOME</span>
                </div>
                {/*
                 * The heading is `aria-hidden`: every row already announces its own full timestamp, and a
                 * screen reader that stopped to read `03:00 — 04:00` before each run would say the hour twice.
                 */}
                {newsFeedGroups(events).map((group) => (
                  <div className="news-event-group" key={group.key}>
                    <div aria-hidden className="news-event-group-heading">
                      <span>{group.label}</span>
                      <span>{formatCount(group.events.length)} 条</span>
                    </div>
                    {group.events.map((event) => (
                      <NewsEventRow
                        event={event}
                        fresh={eventFeed.freshIds.has(event.event_id)}
                        key={event.event_id}
                        onOpen={
                          wideEnoughForDrawer
                            ? (eventId, trigger) => {
                                drawerTriggerRef.current = trigger;
                                setDrawerId(eventId);
                              }
                            : undefined
                        }
                        quotes={quotes}
                        searchState={feedSearch}
                      />
                    ))}
                  </div>
                ))}
              </div>
              {historyQuery.hasNextPage || (!historyRequested && historyCursor) ? (
                <ActionButton
                  className="news-load-more"
                  disabled={historyQuery.isFetchingNextPage}
                  onClick={() => {
                    if (!historyRequested) setHistoryRequested(true);
                    else void historyQuery.fetchNextPage();
                  }}
                >
                  {historyQuery.isFetchingNextPage ? "正在加载" : "加载更多事件"}
                </ActionButton>
              ) : null}
            </div>
          </NewsQuoteReadState>
        </PageState.Stale>
      ) : null}

      <NewsEventDrawer
        eventId={wideEnoughForDrawer ? drawerId : null}
        feedSearch={feedSearch}
        onClose={() => setDrawerId(null)}
        restoreFocusTo={drawerTriggerRef.current}
        token={token}
      />
    </PageShell>
  );
}

function NewsSearchScope({
  filters,
  search,
}: {
  filters: NewsFeedFilters;
  search: NewsFeedSearch;
}) {
  const subject = search.mode === "asset" ? assetSearchSubject(search) : search.normalized_query;
  return (
    <div aria-label="当前检索" className="news-search-scope" role="region">
      <b>{search.mode === "asset" ? `按标的匹配：${subject}` : `全文匹配：${subject}`}</b>
      <span>{searchScope(filters)}</span>
    </div>
  );
}

function assetSearchSubject(search: NewsFeedSearch): string {
  const resolved = search.resolved_symbols.join(" / ");
  if (!resolved || resolved === search.normalized_query) return search.normalized_query;
  return `${search.normalized_query} → ${resolved}`;
}

function searchScope(filters: NewsFeedFilters): string {
  const outcome = filters.outcome === null ? "全部结果" : `${outcomeTabLabel(filters.outcome)}结果`;
  return `${outcome} · ${hoursLabel(filters.hours)}`;
}

function emptyTitle(filters: NewsFeedFilters, search: NewsFeedSearch | null): string {
  if (search?.mode === "asset") {
    const subject = search.resolved_symbols.join(" / ") || search.normalized_query;
    return `当前范围没有 ${subject} 的精确标的命中`;
  }
  if (search?.mode === "text") return "当前范围没有全文命中";
  if (filters.outcome === "pushed") return `${hoursLabel(filters.hours)}没有推送`;
  if (filters.outcome === "held") return `${hoursLabel(filters.hours)}没有被拦截的事件`;
  if (filters.outcome === "pending") return "没有正在处理的事件";
  return `${hoursLabel(filters.hours)}没有匹配的事件`;
}

function emptyHint(filters: NewsFeedFilters, search: NewsFeedSearch | null): string {
  if (search?.mode === "asset") {
    const subject = search.resolved_symbols.join(" / ") || search.normalized_query;
    return `已按标的 ${subject} 精确查询；${searchScope(filters)}内没有关联 Event。`;
  }
  if (search?.mode === "text") {
    return `已按全文检索“${search.normalized_query}”；${searchScope(filters)}内没有文字命中。`;
  }
  return hasAdvancedFilters(filters) || filters.outcome !== null || filters.hours !== 24
    ? "换个时间范围、切到「全部」标签或减少筛选条件后再试。"
    : "事件会在 Strategy 命中并完成入库后出现。";
}
