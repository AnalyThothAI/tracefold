import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { newsFeedIdentity } from "@shared/query/queryKeys";
import { newsEventPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import * as PageState from "@shared/ui/PageState";
import { Fragment, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import {
  type NewsFeedEvent,
  type NewsFeedFilters,
  type NewsFeedOutcome,
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
  formatCount,
  hourBucketKey,
  hourBucketLabel,
  hoursLabel,
  labelCommand,
} from "../../model/newsLabels";
import { useAnchoredEventFeed } from "../../state/useAnchoredEventFeed";
import { useFeedCursor } from "../../state/useFeedCursor";
import { NewsPageHeader, NewsPageShell, NewsPageStamp } from "../chrome/NewsChrome";
import { NewsHealthPill } from "../chrome/NewsHealthPill";
import { NewsEventDrawer } from "../detail/NewsEventDrawer";

import { NewsEventRow } from "./NewsEventRow";
import { NewsActiveFilterChips, NewsFeedToolbar } from "./NewsFeedToolbar";
import { NewsFunnelCard } from "./NewsFunnelCard";

import "./newsFeed.css";

/** Tab order matches the digits 1–4 the shortcut panel advertises. */
const TAB_ORDER: Array<NewsFeedOutcome | null> = [null, "pushed", "held", "pending"];
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
export function NewsFeedPage({
  copy,
  token,
}: {
  copy: (text: string, note: string) => void;
  token: string;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
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
  const eventIds = events.map((event) => event.event_id);
  // One quote request for everything on screen (#88): the symbols the server already resolved, deduplicated
  // into a single query key. Prices never travel in the feed body — that would make its ETag useless.
  const quotesQuery = useNewsQuotesWithToken(
    token,
    events.flatMap((event) => (event.assets ?? []).filter((a) => a.listed).map((a) => a.symbol)),
  );
  const quotes = Object.fromEntries(
    (quotesQuery.data?.quotes ?? []).map((quote) => [quote.requested_symbol, quote]),
  );
  const feedSearch = searchParams.toString();
  const wideEnoughForDrawer = useMediaQuery(DRAWER_QUERY);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [drawerId, setDrawerId] = useState<string | null>(null);
  const selection = useRowSelection(eventIds);

  const updateFeedParams = (changes: FeedFilterChanges) => {
    setSearchParams(nextFeedParams(searchParams, filters, changes), { replace: true });
  };

  const toggleExpanded = (eventId: string) =>
    setExpandedId((current) => (current === eventId ? null : eventId));

  // What the keyboard reaches for. These close over this render's filters, so they live behind a ref and the
  // listeners register once instead of on every three-second poll.
  const actions = useRef({ selectTab: (_index: number) => {} });
  actions.current = {
    selectTab: (index) => updateFeedParams({ outcome: TAB_ORDER[index] }),
  };
  const { cursor, focusEvent } = useFeedCursor({
    enabled: events.length > 0,
    eventIds,
    listRef: eventListRef,
    onActivate: (eventId) => {
      // The drawer follows the cursor while it is open, so `Enter` on a later row simply moves it.
      if (wideEnoughForDrawer) setDrawerId(eventId);
      else navigate(newsEventPath(eventId), { state: { feedSearch } });
    },
    onExpand: toggleExpanded,
    onLabel: (eventId) => copy(labelCommand(eventId, "noise"), "已复制「不该推」标注命令"),
  });
  // J/K with the drawer open walks the list without closing it: the reader is reading through a queue.
  useEffect(() => {
    if (drawerId && cursor && cursor !== drawerId) setDrawerId(cursor);
  }, [cursor, drawerId]);

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

  const groups = groupByHour(events, filters.sort === "latest");
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
                    priority: null,
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
                      cursor={event.event_id === cursor}
                      event={event}
                      expanded={expandedId === event.event_id}
                      fresh={eventFeed.freshIds.has(event.event_id)}
                      key={event.event_id}
                      onExpand={toggleExpanded}
                      onOpen={
                        wideEnoughForDrawer
                          ? (eventId) => {
                              focusEvent(eventId);
                              setDrawerId(eventId);
                            }
                          : undefined
                      }
                      onSelect={selection.toggle}
                      quotes={quotes}
                      searchState={feedSearch}
                      selectable={wideEnoughForDrawer}
                      selected={selection.ids.has(event.event_id)}
                    />
                  ))}
                </Fragment>
              ))}
            </div>
            {selection.ids.size ? (
              <div className="news-selection-bar" role="group" aria-label="批量标注">
                <span className="news-selection-count">已选 {selection.ids.size} 条</span>
                <span className="news-selection-actions">
                  <ActionButton
                    onClick={() => {
                      copy(
                        [...selection.ids].map((id) => labelCommand(id, "missed")).join("\n"),
                        `已复制 ${selection.ids.size} 条「漏推」标注命令`,
                      );
                      selection.clear();
                    }}
                    size="sm"
                    variant="primary"
                  >
                    批量标为漏推
                  </ActionButton>
                  <ActionButton onClick={selection.clear} size="sm">
                    清除
                  </ActionButton>
                </span>
              </div>
            ) : null}
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
        copy={copy}
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
 * Only when the list is chronological: `sort=priority` interleaves hours by design, and a heading over a
 * non-chronological run would claim an order the server did not produce. The grouping is presentational —
 * the rows, their order and their count all come from the server exactly as they arrived.
 */
function groupByHour(events: NewsFeedEvent[], enabled: boolean): FeedGroup[] {
  if (!enabled) return [{ events, key: "all", label: "" }];
  const groups: FeedGroup[] = [];
  for (const event of events) {
    const key = hourBucketKey(event.opened_at_ms);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.events.push(event);
    else groups.push({ events: [event], key, label: hourBucketLabel(event.opened_at_ms) });
  }
  return groups;
}

/**
 * Which rows are selected for bulk labelling (design proposal ④), with `Shift` extending from the last one
 * touched. 484 held Events waiting to be checked is a queue, and clicking a button per row is not a review.
 *
 * Selection is per-render list state and deliberately not in the URL: it is a scratch pad for the next
 * clipboard copy, not something to share. Nothing here writes to the server — the bar copies CLI commands.
 */
function useRowSelection(eventIds: string[]) {
  const [ids, setIds] = useState<ReadonlySet<string>>(() => new Set<string>());
  const anchor = useRef<string | null>(null);
  const toggle = (eventId: string, shiftKey: boolean) => {
    /*
     * The anchor is resolved *before* the updater runs and never read inside it. React may invoke a state
     * updater more than once for the same event, and a ref read in there sees whatever the last call left
     * behind — which turned `Shift` over five rows into a two-row selection.
     */
    const from = anchor.current ? eventIds.indexOf(anchor.current) : -1;
    const to = eventIds.indexOf(eventId);
    anchor.current = eventId;
    setIds((current) => {
      const next = new Set(current);
      if (shiftKey && from >= 0 && to >= 0) {
        for (const id of eventIds.slice(Math.min(from, to), Math.max(from, to) + 1)) next.add(id);
      } else if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  };
  return {
    clear: () => {
      anchor.current = null;
      setIds(new Set<string>());
    },
    ids,
    toggle,
  };
}

function emptyTitle(filters: NewsFeedFilters): string {
  if (filters.outcome === "pushed") return `${hoursLabel(filters.hours)}没有推送`;
  if (filters.outcome === "held") return `${hoursLabel(filters.hours)}没有被拦截的事件`;
  if (filters.outcome === "pending") return "没有正在处理的事件";
  return `${hoursLabel(filters.hours)}没有匹配的事件`;
}
