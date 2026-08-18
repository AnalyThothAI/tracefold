import * as PageState from "@shared/ui/PageState";
import { SlidersHorizontal } from "lucide-react";
import { type RefObject, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import "./news.css";
import "./newsFeed.css";
import { NewsEventDetailPage } from "./NewsEventDetailPage";
import { NewsEventRow } from "./NewsEventRow";
import { NewsSectionTabs } from "./NewsSectionTabs";
import { NewsStatusPage } from "./NewsStatusPage";
import {
  admissionLabel,
  decisionLabel,
  familyLabel,
  optionalDuration,
  priorityLabel,
} from "./newsLabels";
import {
  NEWS_FEED_DECISIONS,
  NEWS_FEED_PRIORITIES,
  type NewsFeedDecision,
  type NewsFeedEvent,
  type NewsFeedFilters,
  type NewsFeedPriority,
  type NewsFeedSort,
  type NewsStatus,
  useNewsFeedHistoryWithToken,
  useNewsFeedWithToken,
  useNewsStatusWithToken,
} from "./useNewsPage";

type NewsPageProps =
  | { token: string; view: "feed" | "status" }
  | { eventId: string; token: string; view: "event" };

type FeedFilterChanges = Partial<Omit<NewsFeedFilters, "q">> & { q?: string | null };

const KNOWN_FAMILIES = ["market_telemetry", "filing", "disaster", "general"] as const;
const KNOWN_ADMISSIONS = [
  "candidate",
  "listing_deterministic",
  "recovery",
  "suppressed_low_signal",
  "suppressed_pr_template",
] as const;

export function NewsPage(props: NewsPageProps) {
  if (props.view === "status") return <NewsStatusPage token={props.token} />;
  if (props.view === "event")
    return <NewsEventDetailPage eventId={props.eventId} token={props.token} />;
  return <FeedRoute token={props.token} />;
}

function FeedRoute({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = parseFeedFilters(searchParams);
  const query = useNewsFeedWithToken(token, filters);
  const statusQuery = useNewsStatusWithToken(token);
  const feedIdentity = [
    filters.q,
    filters.family ?? "",
    filters.admission ?? "",
    filters.priority ?? "",
    filters.decision ?? "",
    filters.symbol ?? "",
    filters.sort,
  ].join("\u001f");
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
  const hasActiveFilters = Boolean(
    filters.q ||
    filters.family ||
    filters.admission ||
    filters.priority ||
    filters.decision ||
    filters.symbol,
  );
  const eventListRef = useRef<HTMLDivElement>(null);
  const eventFeed = useAnchoredEventFeed(
    eventListRef,
    serverEvents,
    firstPage?.events ?? [],
    feedIdentity,
  );
  const events = eventFeed.events;

  const updateFeedParams = (changes: FeedFilterChanges) => {
    const params = new URLSearchParams(searchParams);
    params.set("sort", changes.sort ?? filters.sort);
    setOptionalParam(params, "q", changes.q === undefined ? filters.q : changes.q);
    setOptionalParam(
      params,
      "family",
      changes.family === undefined ? filters.family : changes.family,
    );
    setOptionalParam(
      params,
      "admission",
      changes.admission === undefined ? filters.admission : changes.admission,
    );
    setOptionalParam(
      params,
      "priority",
      changes.priority === undefined ? filters.priority : changes.priority,
    );
    setOptionalParam(
      params,
      "decision",
      changes.decision === undefined ? filters.decision : changes.decision,
    );
    setOptionalParam(
      params,
      "symbol",
      changes.symbol === undefined ? filters.symbol : changes.symbol,
    );
    setSearchParams(params, { replace: true });
  };

  const resetFilters = () =>
    updateFeedParams({
      admission: null,
      decision: null,
      family: null,
      priority: null,
      q: null,
      symbol: null,
    });

  return (
    <section
      aria-label="新闻事件流"
      className="news-panel news-feed-shell"
      data-feed-density="compact"
      data-page-archetype="scan"
    >
      <header className="news-feed-header">
        <div className="news-heading-copy">
          <h1>新闻事件流</h1>
          <p>账户 Strategy 命中的新闻事件、Triage 判定与推送状态</p>
        </div>
        <NewsInlineStatus
          error={statusQuery.isError}
          fetching={statusQuery.isFetching}
          status={statusQuery.data}
        />
      </header>

      <div className="news-feed-toolbar">
        <NewsSectionTabs active="feed" />
        <NewsFeedControls filters={filters} onChange={updateFeedParams} />
      </div>

      <ActiveFilterChips filters={filters} onRemove={updateFeedParams} />

      {query.isLoading && !query.data ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取新闻事件" />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {!query.isLoading && !query.isError && !events.length ? (
        <PageState.Empty
          action={
            hasActiveFilters ? (
              <button className="news-reset-button" onClick={resetFilters} type="button">
                清除筛选
              </button>
            ) : null
          }
          hint={
            hasActiveFilters
              ? "换个关键词或减少筛选条件后再试。"
              : "事件会在 Strategy 命中并完成入库后出现。"
          }
          title={hasActiveFilters ? "没有匹配的事件" : "暂无事件"}
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
                <NewsEventRow event={event} key={event.event_id} />
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
    </section>
  );
}

function NewsFeedControls({
  filters,
  onChange,
}: {
  filters: NewsFeedFilters;
  onChange: (changes: FeedFilterChanges) => void;
}) {
  const [symbolDraft, setSymbolDraft] = useState(filters.symbol ?? "");
  useEffect(() => {
    setSymbolDraft(filters.symbol ?? "");
  }, [filters.symbol]);
  return (
    <div className="news-filter-bar">
      <label className="news-sort-control">
        <span>排序</span>
        <select
          aria-label="事件排序"
          onChange={(event) => onChange({ sort: parseSort(event.target.value) })}
          value={filters.sort}
        >
          <option value="latest">最新</option>
          <option value="priority">优先级</option>
        </select>
      </label>
      <details className="news-filter-disclosure">
        <summary>
          <SlidersHorizontal aria-hidden />
          筛选
        </summary>
        <div>
          <label>
            <span>家族</span>
            <select
              aria-label="事件家族"
              onChange={(event) => onChange({ family: event.target.value || null })}
              value={filters.family ?? ""}
            >
              <option value="">全部家族</option>
              {withSelectedOption(KNOWN_FAMILIES, filters.family).map((value) => (
                <option key={value} value={value}>
                  {familyLabel(value)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>准入</span>
            <select
              aria-label="事件准入"
              onChange={(event) => onChange({ admission: event.target.value || null })}
              value={filters.admission ?? ""}
            >
              <option value="">全部准入</option>
              {withSelectedOption(KNOWN_ADMISSIONS, filters.admission).map((value) => (
                <option key={value} value={value}>
                  {admissionLabel(value)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>优先级</span>
            <select
              aria-label="事件优先级"
              onChange={(event) => onChange({ priority: parsePriority(event.target.value) })}
              value={filters.priority ?? ""}
            >
              <option value="">全部优先级</option>
              {NEWS_FEED_PRIORITIES.map((value) => (
                <option key={value} value={value}>
                  {priorityLabel(value)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>判定</span>
            <select
              aria-label="Triage 判定"
              onChange={(event) => onChange({ decision: parseDecision(event.target.value) })}
              value={filters.decision ?? ""}
            >
              <option value="">全部判定</option>
              {NEWS_FEED_DECISIONS.map((value) => (
                <option key={value} value={value}>
                  {decisionLabel(value)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>资产</span>
            <input
              aria-label="落地资产"
              autoCapitalize="characters"
              maxLength={16}
              onBlur={() => onChange({ symbol: normalizeSymbol(symbolDraft) })}
              onChange={(event) => setSymbolDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  onChange({ symbol: normalizeSymbol(symbolDraft) });
                }
              }}
              placeholder="BTC"
              value={symbolDraft}
            />
          </label>
        </div>
      </details>
    </div>
  );
}

function ActiveFilterChips({
  filters,
  onRemove,
}: {
  filters: NewsFeedFilters;
  onRemove: (changes: FeedFilterChanges) => void;
}) {
  const chips = [
    filters.q ? { label: `搜索：${filters.q}`, remove: () => onRemove({ q: null }) } : null,
    filters.family
      ? { label: `家族：${familyLabel(filters.family)}`, remove: () => onRemove({ family: null }) }
      : null,
    filters.admission
      ? {
          label: `准入：${admissionLabel(filters.admission)}`,
          remove: () => onRemove({ admission: null }),
        }
      : null,
    filters.priority
      ? {
          label: `优先级：${priorityLabel(filters.priority)}`,
          remove: () => onRemove({ priority: null }),
        }
      : null,
    filters.decision
      ? {
          label: `判定：${decisionLabel(filters.decision)}`,
          remove: () => onRemove({ decision: null }),
        }
      : null,
    filters.symbol
      ? { label: `资产：${filters.symbol}`, remove: () => onRemove({ symbol: null }) }
      : null,
  ].filter((chip): chip is { label: string; remove: () => void } => chip !== null);
  if (!chips.length) return null;
  return (
    <div aria-label="已启用筛选" className="news-active-filters" role="group">
      {chips.map((chip) => (
        <button
          aria-label={`移除${chip.label}`}
          key={chip.label}
          onClick={chip.remove}
          type="button"
        >
          {chip.label}
          <span aria-hidden>×</span>
        </button>
      ))}
    </div>
  );
}

function NewsInlineStatus({
  error,
  fetching,
  status,
}: {
  error: boolean;
  fetching: boolean;
  status?: NewsStatus;
}) {
  const readerStatus = readerFacingNewsStatus(error, status);
  return (
    <div className="news-inline-status" data-state={readerStatus.state} role="status">
      <span aria-hidden className="news-status-dot" />
      <span>
        <b>{readerStatus.label}</b>
        <small>{inlineStatusSummary(status, fetching)}</small>
      </span>
    </div>
  );
}

function readerFacingNewsStatus(
  error: boolean,
  status: NewsStatus | undefined,
): { label: string; state: "live" | "recovering" | "stalled" } {
  if (error) return { label: "状态暂不可用", state: "stalled" };
  if (!status) return { label: "正在检查 WSS", state: "recovering" };
  if (!status.ingest.connected) return { label: "WSS 未连接", state: "stalled" };
  if (status.state === "ready") return { label: "WSS 已连接", state: "live" };
  if (status.state === "unavailable") return { label: "流水线不可用", state: "stalled" };
  return {
    label: status.state === "degraded" ? "流水线降级" : "流水线预热中",
    state: "recovering",
  };
}

function inlineStatusSummary(status: NewsStatus | undefined, fetching: boolean): string {
  if (!status) return fetching ? "正在检查" : "等待首次状态";
  const prefix = fetching ? "刷新中 · " : "";
  const pipeline = status.pipeline;
  return `${prefix}1h 事件 ${pipeline.events_1h} · Triage P95 ${optionalDuration(pipeline.triage_p95_ms)} · 24h 推送 ${status.delivery.sent_24h}`;
}

function parseFeedFilters(searchParams: URLSearchParams): NewsFeedFilters {
  return {
    admission: searchParams.get("admission") || null,
    decision: parseDecision(searchParams.get("decision")),
    family: searchParams.get("family") || null,
    priority: parsePriority(searchParams.get("priority")),
    q: searchParams.get("q")?.trim() ?? "",
    sort: parseSort(searchParams.get("sort")),
    symbol: normalizeSymbol(searchParams.get("symbol")),
  };
}

function parseSort(value: string | null): NewsFeedSort {
  return value === "priority" ? "priority" : "latest";
}

function parsePriority(value: string | null): NewsFeedPriority | null {
  return NEWS_FEED_PRIORITIES.find((candidate) => candidate === value) ?? null;
}

function parseDecision(value: string | null): NewsFeedDecision | null {
  return NEWS_FEED_DECISIONS.find((candidate) => candidate === value) ?? null;
}

function normalizeSymbol(value: string | null | undefined): string | null {
  const normalized = value?.trim().toUpperCase() ?? "";
  return normalized ? normalized.slice(0, 16) : null;
}

function setOptionalParam(params: URLSearchParams, name: string, value: string | null | undefined) {
  if (value) params.set(name, value);
  else params.delete(name);
}

function withSelectedOption(options: readonly string[], selected: string | null): string[] {
  if (!selected || options.includes(selected)) return [...options];
  return [selected, ...options];
}

function useAnchoredEventFeed(
  listRef: RefObject<HTMLDivElement | null>,
  serverEvents: NewsFeedEvent[],
  firstPageEvents: NewsFeedEvent[],
  identity: string,
) {
  const [count, setCount] = useState(0);
  const [, setRevision] = useState(0);
  const acceptedEventsRef = useRef<NewsFeedEvent[]>(serverEvents);
  const awayFromTopRef = useRef(false);
  const deferredTopIdsRef = useRef<Set<string>>(new Set());
  const deferredRef = useRef(false);
  const identityRef = useRef<string | null>(null);
  const knownIdsRef = useRef<Set<string>>(new Set());
  const latestEventsRef = useRef<NewsFeedEvent[]>(serverEvents);
  const scrollContainerRef = useRef<HTMLElement | null>(null);
  const firstPageKey = firstPageEvents.map((event) => event.event_id).join("\u001f");
  latestEventsRef.current = serverEvents;
  const identityChanged = identityRef.current !== identity;
  const addedTopIds = identityChanged
    ? []
    : firstPageEvents.flatMap((event) =>
        knownIdsRef.current.has(event.event_id) ? [] : [event.event_id],
      );
  const startsDeferral = addedTopIds.length > 0 && awayFromTopRef.current;
  const shouldDefer = deferredRef.current || startsDeferral;
  const excludedTopIds = startsDeferral
    ? new Set([...deferredTopIdsRef.current, ...addedTopIds])
    : deferredTopIdsRef.current;
  const events = shouldDefer
    ? appendNonDeferredTail(acceptedEventsRef.current, serverEvents, excludedTopIds)
    : serverEvents;

  useEffect(() => {
    const scrollContainer =
      listRef.current?.closest<HTMLElement>(".center-column") ?? document.documentElement;
    scrollContainerRef.current = scrollContainer;
    const handleScroll = () => {
      awayFromTopRef.current = scrollContainer.scrollTop > 96;
      if (!awayFromTopRef.current && deferredRef.current) {
        acceptedEventsRef.current = latestEventsRef.current;
        deferredTopIdsRef.current.clear();
        deferredRef.current = false;
        setCount(0);
        setRevision((current) => current + 1);
      }
    };
    handleScroll();
    scrollContainer.addEventListener("scroll", handleScroll, { passive: true });
    return () => scrollContainer.removeEventListener("scroll", handleScroll);
  }, [firstPageKey, listRef]);

  useEffect(() => {
    const currentIds = new Set(firstPageEvents.map((event) => event.event_id));
    if (identityRef.current !== identity) {
      identityRef.current = identity;
      knownIdsRef.current = currentIds;
      acceptedEventsRef.current = serverEvents;
      deferredTopIdsRef.current.clear();
      deferredRef.current = false;
      setCount(0);
      return;
    }
    const newlyAddedIds = firstPageEvents.flatMap((event) =>
      knownIdsRef.current.has(event.event_id) ? [] : [event.event_id],
    );
    knownIdsRef.current = currentIds;
    if (newlyAddedIds.length && awayFromTopRef.current) {
      let newlyDeferred = 0;
      for (const eventId of newlyAddedIds) {
        if (deferredTopIdsRef.current.has(eventId)) continue;
        deferredTopIdsRef.current.add(eventId);
        newlyDeferred += 1;
      }
      deferredRef.current = true;
      if (newlyDeferred) setCount((current) => current + newlyDeferred);
      return;
    }
    if (!deferredRef.current) acceptedEventsRef.current = serverEvents;
  }, [firstPageKey, firstPageEvents, identity, serverEvents]);

  const reveal = () => {
    const scrollContainer = scrollContainerRef.current;
    acceptedEventsRef.current = latestEventsRef.current;
    deferredTopIdsRef.current.clear();
    deferredRef.current = false;
    setRevision((current) => current + 1);
    if (scrollContainer && typeof scrollContainer.scrollTo === "function") {
      scrollContainer.scrollTo({ behavior: "smooth", top: 0 });
    } else if (scrollContainer) {
      scrollContainer.scrollTop = 0;
    }
    awayFromTopRef.current = false;
    setCount(0);
  };

  return { count, events, reveal };
}

function appendNonDeferredTail(
  acceptedEvents: NewsFeedEvent[],
  serverEvents: NewsFeedEvent[],
  deferredTopIds: Set<string>,
): NewsFeedEvent[] {
  const seen = new Set(acceptedEvents.map((event) => event.event_id));
  const appended = serverEvents.filter((event) => {
    if (deferredTopIds.has(event.event_id) || seen.has(event.event_id)) return false;
    seen.add(event.event_id);
    return true;
  });
  return appended.length ? [...acceptedEvents, ...appended] : acceptedEvents;
}
