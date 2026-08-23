import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { newsFeedIdentity } from "@shared/query/queryKeys";
import { ActionButton } from "@shared/ui/ActionButton";
import * as PageState from "@shared/ui/PageState";
import { Fragment, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  type NewsFeedEvent,
  type NewsFeedFilters,
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
import {
  absoluteTime,
  dayBucketLabel,
  formatCount,
  hourBucketKey,
  hourBucketLabel,
  hoursLabel,
} from "../../model/newsLabels";
import { useAnchoredEventFeed } from "../../state/useAnchoredEventFeed";
import { NewsPageHeader, NewsPageShell, NewsPageStamp } from "../chrome/NewsChrome";
import { NewsHealthPill } from "../chrome/NewsHealthPill";
import { NewsEventDrawer } from "../detail/NewsEventDrawer";

import { NewsEventRow } from "./NewsEventRow";
import { NewsActiveFilterChips, NewsFeedToolbar } from "./NewsFeedToolbar";
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
 * Four things happen on top of that, all of them about not losing the reader's place: hour headings so two
 * screens of scrolling still say when you are, held-back inserts so a live stream never shifts the row under
 * the pointer, an in-place judgment so "why was this dropped" costs no navigation, and a drawer so opening an
 * Event does not replace the list you were working through.
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
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [drawerId, setDrawerId] = useState<string | null>(null);

  const updateFeedParams = (changes: FeedFilterChanges) => {
    setSearchParams(nextFeedParams(filters, changes), { replace: true });
  };

  const toggleExpanded = (eventId: string) =>
    setExpandedId((current) => (current === eventId ? null : eventId));

  const groups = groupByHour(events);
  return (
    <NewsPageShell archetype="scan" className="news-feed-shell" label="新闻事件流">
      <NewsPageHeader subtitle="每条新闻的判定与去向，动作都在详情页。" title="新闻事件流">
        <NewsHealthPill error={statusQuery.isError} status={statusQuery.data} />
        {statusQuery.data ? (
          <NewsPageStamp>
            更新于 {absoluteTime(statusQuery.data.measured_at_ms).slice(11)}
          </NewsPageStamp>
        ) : null}
      </NewsPageHeader>

      <NewsFunnelCard status={statusQuery.data} />

      <NewsFeedToolbar
        counts={firstPage?.counts ?? undefined}
        filters={filters}
        hasAdvanced={hasAdvanced}
        onChange={updateFeedParams}
        visibleCount={events.length}
      />

      <NewsActiveFilterChips filters={filters} onRemove={updateFeedParams} />

      {query.isLoading && !query.data ? (
        <PageState.Loading label="正在读取新闻事件" layout="panel" rows={6} />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {!query.isLoading && !query.isError && !events.length ? (
        <PageState.Empty
          action={
            hasAdvanced || filters.hours != null ? (
              <ActionButton
                onClick={() =>
                  updateFeedParams({
                    admission: null,
                    decision: null,
                    family: null,
                    hours: null,
                    q: null,
                    symbol: null,
                  })
                }
              >
                清除筛选，查看全部时间
              </ActionButton>
            ) : null
          }
          hint={
            hasAdvanced || filters.outcome || filters.hours != null
              ? "换个时间范围、切到「全部」标签或减少筛选条件后再试。"
              : "事件会在 Strategy 命中并完成入库后出现。"
          }
          title={emptyTitle(filters)}
        />
      ) : null}
      {events.length ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <div className="news-feed-results">
            {eventFeed.count ? (
              <button className="news-new-events" onClick={eventFeed.reveal} type="button">
                <span aria-hidden className="news-new-events-dot" />
                {eventFeed.count} 条新事件 · 回到顶部
              </button>
            ) : null}
            <div className="news-event-list" ref={eventListRef}>
              {groups.map((group) => (
                <Fragment key={group.key}>
                  {group.label ? (
                    <div className="news-hour-group">
                      <span className="news-hour-group-label">{group.label}</span>
                      <span className="news-hour-group-meta">
                        {formatCount(group.events.length)} 条 · 推送{" "}
                        {formatCount(
                          group.events.filter((event) => event.outcome.group === "pushed").length,
                        )}
                      </span>
                    </div>
                  ) : null}
                  {group.events.map((event) => (
                    <NewsEventRow
                      event={event}
                      expanded={expandedId === event.event_id}
                      fresh={eventFeed.freshIds.has(event.event_id)}
                      key={event.event_id}
                      onExpand={toggleExpanded}
                      onOpen={wideEnoughForDrawer ? setDrawerId : undefined}
                      quotes={quotes}
                      searchState={feedSearch}
                      selectable={false}
                      selected={false}
                    />
                  ))}
                </Fragment>
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
        </PageState.Stale>
      ) : null}

      <NewsEventDrawer
        eventId={wideEnoughForDrawer ? drawerId : null}
        feedSearch={feedSearch}
        onClose={() => setDrawerId(null)}
        token={token}
      />
    </NewsPageShell>
  );
}

type FeedGroup = { events: NewsFeedEvent[]; key: string; label: string };

/**
 * Consecutive Events bucketed by the hour they were published (design proposal ⑤).
 *
 * The grouping is presentational — the rows, their order and their count all come from the server exactly
 * as they arrived.
 */
function groupByHour(events: NewsFeedEvent[]): FeedGroup[] {
  const groups: FeedGroup[] = [];
  let day: string | null = null;
  for (const event of events) {
    const key = hourBucketKey(event.opened_at_ms);
    const last = groups[groups.length - 1];
    if (last && last.key === key) {
      last.events.push(event);
      continue;
    }
    // The date appears on the first group of each day and nowhere else: enough to place a run of hours,
    // quiet enough that it does not repeat down a screenful from the same afternoon.
    const eventDay = dayBucketLabel(event.opened_at_ms);
    groups.push({
      events: [event],
      key,
      label: hourBucketLabel(event.opened_at_ms, day !== null && eventDay !== day),
    });
    day = eventDay;
  }
  return groups;
}

function emptyTitle(filters: NewsFeedFilters): string {
  if (filters.outcome === "pushed") return `${hoursLabel(filters.hours)}没有推送`;
  if (filters.outcome === "held") return `${hoursLabel(filters.hours)}没有被拦截的事件`;
  if (filters.outcome === "pending") return "没有正在处理的事件";
  return `${hoursLabel(filters.hours)}没有匹配的事件`;
}
