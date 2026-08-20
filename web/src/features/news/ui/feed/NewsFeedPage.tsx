import { newsFeedIdentity } from "@shared/query/queryKeys";
import { newsEventPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import {
  type NewsFeedFilters,
  type NewsFeedOutcome,
  useNewsFeedHistoryWithToken,
  useNewsFeedWithToken,
  useNewsStatusWithToken,
} from "../../api/newsQueries";
import {
  hasAdvancedFilters,
  nextFeedParams,
  parseFeedFilters,
  type FeedFilterChanges,
} from "../../model/feedFilters";
import { hoursLabel, labelCommand } from "../../model/newsLabels";
import { useAnchoredEventFeed } from "../../state/useAnchoredEventFeed";
import { useFeedCursor } from "../../state/useFeedCursor";
import { useNewsToast } from "../../state/useNewsToast";
import { NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";
import { NewsHealthPill } from "../chrome/NewsHealthPill";
import { NewsToast } from "../chrome/NewsToast";

import { NewsEventRow } from "./NewsEventRow";
import { NewsActiveFilterChips, NewsFeedToolbar } from "./NewsFeedToolbar";
import { NewsFunnelCard } from "./NewsFunnelCard";

import "./newsFeed.css";

/**
 * The decision-first scan surface over the flat Event feed. The browser never clusters, scores, triages,
 * throttles or reorders — it asks the server for a page and renders one row per Event.
 */
export function NewsFeedPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const filters = parseFeedFilters(searchParams);
  const query = useNewsFeedWithToken(token, filters);
  const statusQuery = useNewsStatusWithToken(token);
  const feedIdentity = newsFeedIdentity(filters).join("\u001f");
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
  const toast = useNewsToast();
  const feedSearch = searchParams.toString();

  const updateFeedParams = (changes: FeedFilterChanges) => {
    setSearchParams(nextFeedParams(searchParams, filters, changes), { replace: true });
  };

  // What the keyboard reaches for. These close over this render's filters, so they live behind a ref and the
  // listeners register once instead of on every three-second poll.
  const actions = useRef({ selectTab: (_index: number) => {} });
  actions.current = {
    selectTab: (index) => updateFeedParams({ outcome: TAB_ORDER[index] }),
  };
  const { cursor } = useFeedCursor({
    enabled: events.length > 0,
    eventIds: events.map((event) => event.event_id),
    listRef: eventListRef,
    onActivate: (eventId) => navigate(newsEventPath(eventId), { state: { feedSearch } }),
    onLabel: (eventId) => toast.copy(labelCommand(eventId, "bad"), "已复制「判错了」标注命令"),
  });

  // Digits pick a task tab from anywhere on the route; the feed cursor owns the rest of the keyboard.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      const index = "1234".indexOf(event.key);
      if (index < 0) return;
      event.preventDefault();
      actions.current.selectTab(index);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <NewsPageShell archetype="scan" className="news-feed-shell" label="新闻事件流">
      <NewsPageHeader
        subtitle="每条新闻只有一个结论：推了、没推为什么、还在处理。"
        title="新闻事件流"
      >
        <NewsHealthPill error={statusQuery.isError} status={statusQuery.data} />
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
        <PageState.Loading layout="panel" rows={6} label="正在读取新闻事件" />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {!query.isLoading && !query.isError && !events.length ? (
        <PageState.Empty
          action={
            hasAdvanced || filters.hours != null ? (
              <button
                className="news-button"
                onClick={() =>
                  updateFeedParams({
                    admission: null,
                    decision: null,
                    family: null,
                    hours: null,
                    priority: null,
                    q: null,
                    symbol: null,
                  })
                }
                type="button"
              >
                清除筛选，查看全部时间
              </button>
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
                {eventFeed.count} 条新事件 · 回到顶部
              </button>
            ) : null}
            <div className="news-event-list" ref={eventListRef}>
              {events.map((event) => (
                <NewsEventRow
                  cursor={event.event_id === cursor}
                  event={event}
                  key={event.event_id}
                  onCopy={toast.copy}
                  searchState={feedSearch}
                />
              ))}
            </div>
            {historyQuery.hasNextPage || (!historyRequested && historyCursor) ? (
              <button
                className="news-load-more"
                disabled={historyQuery.isFetchingNextPage}
                onClick={() => {
                  if (!historyRequested) setHistoryRequested(true);
                  else void historyQuery.fetchNextPage();
                }}
                type="button"
              >
                {historyQuery.isFetchingNextPage ? "正在加载" : "加载更多事件"}
              </button>
            ) : null}
          </div>
        </PageState.Stale>
      ) : null}
      <NewsToast message={toast.message} />
    </NewsPageShell>
  );
}

/** Tab order matches the digits 1–4 the toolbar advertises. */
const TAB_ORDER: Array<NewsFeedOutcome | null> = [null, "pushed", "held", "pending"];

function emptyTitle(filters: NewsFeedFilters): string {
  if (filters.outcome === "pushed") return `${hoursLabel(filters.hours)}没有推送`;
  if (filters.outcome === "held") return `${hoursLabel(filters.hours)}没有被拦截的事件`;
  if (filters.outcome === "pending") return "没有正在处理的事件";
  return `${hoursLabel(filters.hours)}没有匹配的事件`;
}
