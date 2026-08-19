import { newsFeedIdentity } from "@shared/query/queryKeys";
import { newsStatusPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { SlidersHorizontal, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import "./news.css";
import "./newsFeed.css";
import { NewsEventDetailPage } from "./NewsEventDetailPage";
import { NewsEventRow } from "./NewsEventRow";
import { NewsSectionTabs } from "./NewsSectionTabs";
import { NewsStatusPage } from "./NewsStatusPage";
import {
  formatCount,
  healthLevelLabel,
  healthTone,
  hoursLabel,
  outcomeTabLabel,
} from "./newsLabels";
import { useAnchoredEventFeed } from "./useAnchoredEventFeed";
import {
  NEWS_FEED_DECISIONS,
  NEWS_FEED_DEFAULT_HOURS,
  NEWS_FEED_HOURS,
  NEWS_FEED_OUTCOMES,
  NEWS_FEED_PRIORITIES,
  type NewsFeedDecision,
  type NewsFeedFilters,
  type NewsFeedOutcome,
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

// Power-user filter copy for the raw enums the feed still accepts (URL-owned; the API validates them).
const FAMILY_FILTER_LABELS: Record<string, string> = {
  market_telemetry: "盘口数据",
  filing: "公告/申报",
  disaster: "灾害",
  general: "综合",
};
const ADMISSION_FILTER_LABELS: Record<string, string> = {
  candidate: "已送审",
  listing_deterministic: "上币/下币公告",
  recovery: "补抄件",
  suppressed_low_signal: "低分噪音（未送审）",
  suppressed_pr_template: "律所模板（未送审）",
};
const PRIORITY_FILTER_LABELS: Record<NewsFeedPriority, string> = {
  high: "高优先级",
  normal: "普通",
};
const DECISION_FILTER_LABELS: Record<NewsFeedDecision, string> = {
  push: "推送",
  escalate: "重点推送",
  drop: "不推",
  throttled: "限流",
  degraded: "降级",
};

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
  const hasAdvancedFilters = Boolean(
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
    setOptionalParam(
      params,
      "outcome",
      changes.outcome === undefined ? filters.outcome : changes.outcome,
    );
    const hours = changes.hours === undefined ? filters.hours : changes.hours;
    params.set("hours", hours == null ? "all" : String(hours));
    setSearchParams(params, { replace: true });
  };

  return (
    <section
      aria-label="新闻事件流"
      className="news-panel news-feed-shell"
      data-feed-density="compact"
      data-page-archetype="scan"
    >
      <header className="news-page-header">
        <div className="news-heading-copy">
          <h1>新闻事件流</h1>
          <p>每条新闻只有一个结论：推了、没推为什么、还在处理。</p>
        </div>
        <NewsHealthPill error={statusQuery.isError} status={statusQuery.data} />
      </header>

      <NewsFunnelStrip status={statusQuery.data} />

      <div className="news-feed-toolbar">
        <div className="news-feed-toolbar-left">
          <NewsSectionTabs active="feed" />
          <OutcomeTabs
            active={filters.outcome}
            onChange={(outcome) => updateFeedParams({ outcome })}
          />
        </div>
        <NewsFeedControls
          filters={filters}
          hasAdvancedFilters={hasAdvancedFilters}
          onChange={updateFeedParams}
        />
      </div>

      <ActiveFilterChips filters={filters} onRemove={updateFeedParams} />

      {query.isLoading && !query.data ? (
        <PageState.Loading layout="panel" rows={6} label="正在读取新闻事件" />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {!query.isLoading && !query.isError && !events.length ? (
        <PageState.Empty
          action={
            hasAdvancedFilters || filters.hours != null ? (
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
            hasAdvancedFilters || filters.outcome || filters.hours != null
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

function OutcomeTabs({
  active,
  onChange,
}: {
  active: NewsFeedOutcome | null;
  onChange: (value: NewsFeedOutcome | null) => void;
}) {
  const options: Array<NewsFeedOutcome | null> = [null, ...NEWS_FEED_OUTCOMES];
  return (
    <div aria-label="按结局筛选" className="news-segmented" role="tablist">
      {options.map((value) => (
        <button
          aria-selected={active === value}
          className="news-segmented-option"
          data-outcome-group={value ?? "all"}
          key={value ?? "all"}
          onClick={() => onChange(value)}
          role="tab"
          type="button"
        >
          {outcomeTabLabel(value)}
        </button>
      ))}
    </div>
  );
}

function NewsFeedControls({
  filters,
  hasAdvancedFilters,
  onChange,
}: {
  filters: NewsFeedFilters;
  hasAdvancedFilters: boolean;
  onChange: (changes: FeedFilterChanges) => void;
}) {
  const [symbolDraft, setSymbolDraft] = useState(filters.symbol ?? "");
  useEffect(() => {
    setSymbolDraft(filters.symbol ?? "");
  }, [filters.symbol]);
  return (
    <div className="news-filter-bar">
      <label className="news-select">
        <span className="sr-only">时间范围</span>
        <select
          aria-label="时间范围"
          onChange={(event) => onChange({ hours: parseHours(event.target.value) })}
          value={filters.hours == null ? "all" : String(filters.hours)}
        >
          {NEWS_FEED_HOURS.map((hours) => (
            <option key={hours} value={String(hours)}>
              {hoursLabel(hours)}
            </option>
          ))}
          <option value="all">全部时间</option>
        </select>
      </label>
      <label className="news-select">
        <span className="sr-only">排序</span>
        <select
          aria-label="事件排序"
          onChange={(event) => onChange({ sort: parseSort(event.target.value) })}
          value={filters.sort}
        >
          <option value="latest">最新在前</option>
          <option value="priority">高优先级在前</option>
        </select>
      </label>
      <details className="news-filter-disclosure">
        <summary data-active={hasAdvancedFilters || undefined}>
          <SlidersHorizontal aria-hidden />
          筛选
        </summary>
        <div>
          <label>
            <span>来源类别</span>
            <select
              aria-label="事件家族"
              onChange={(event) => onChange({ family: event.target.value || null })}
              value={filters.family ?? ""}
            >
              <option value="">全部</option>
              {withSelectedOption(KNOWN_FAMILIES, filters.family).map((value) => (
                <option key={value} value={value}>
                  {FAMILY_FILTER_LABELS[value] ?? value}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>门禁</span>
            <select
              aria-label="事件准入"
              onChange={(event) => onChange({ admission: event.target.value || null })}
              value={filters.admission ?? ""}
            >
              <option value="">全部</option>
              {withSelectedOption(KNOWN_ADMISSIONS, filters.admission).map((value) => (
                <option key={value} value={value}>
                  {ADMISSION_FILTER_LABELS[value] ?? value}
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
              <option value="">全部</option>
              {NEWS_FEED_PRIORITIES.map((value) => (
                <option key={value} value={value}>
                  {PRIORITY_FILTER_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>决策</span>
            <select
              aria-label="Triage 判定"
              onChange={(event) => onChange({ decision: parseDecision(event.target.value) })}
              value={filters.decision ?? ""}
            >
              <option value="">全部</option>
              {NEWS_FEED_DECISIONS.map((value) => (
                <option key={value} value={value}>
                  {DECISION_FILTER_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>资产代码</span>
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
      ? {
          label: `来源类别：${FAMILY_FILTER_LABELS[filters.family] ?? filters.family}`,
          remove: () => onRemove({ family: null }),
        }
      : null,
    filters.admission
      ? {
          label: `门禁：${ADMISSION_FILTER_LABELS[filters.admission] ?? filters.admission}`,
          remove: () => onRemove({ admission: null }),
        }
      : null,
    filters.priority
      ? {
          label: `优先级：${PRIORITY_FILTER_LABELS[filters.priority]}`,
          remove: () => onRemove({ priority: null }),
        }
      : null,
    filters.decision
      ? {
          label: `决策：${DECISION_FILTER_LABELS[filters.decision]}`,
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
          <X aria-hidden />
        </button>
      ))}
    </div>
  );
}

function NewsHealthPill({ error, status }: { error: boolean; status?: NewsStatus }) {
  if (error) {
    return (
      <Link className="news-health-pill news-toned" data-tone="alert" to={newsStatusPath()}>
        <span aria-hidden className="news-outcome-dot" />
        <b>状态暂不可用</b>
      </Link>
    );
  }
  // `health` is required by the contract; the guard only covers the seconds of a rolling deploy where the
  // console is newer than the API, so the feed keeps rendering instead of throwing.
  const health = status?.health;
  if (!status || !health) {
    return (
      <span className="news-health-pill news-toned" data-tone="neutral" role="status">
        <span aria-hidden className="news-outcome-dot" />
        <b>正在检查流水线</b>
      </span>
    );
  }
  const level = health.overall;
  const worst = (["ingest", "broker", "model", "delivery"] as const)
    .map((key) => health[key])
    .find((item) => item.level === level);
  return (
    <Link
      aria-label="查看流水线状态"
      className="news-health-pill news-toned"
      data-tone={healthTone(level)}
      to={newsStatusPath()}
    >
      <span aria-hidden className="news-outcome-dot" />
      <b>流水线{healthLevelLabel(level)}</b>
      <small>{worst?.summary_zh ?? ""}</small>
    </Link>
  );
}

function NewsFunnelStrip({ status }: { status?: NewsStatus }) {
  const funnel = status?.funnel_24h;
  if (!funnel) return null;
  const cells = [
    ["收到", funnel.received, `最近 1 小时 ${formatCount(funnel.received_1h)}`],
    ["送审", funnel.triaged, `候选 ${formatCount(funnel.candidates)}`],
    ["决定推送", funnel.decided_push, ""],
    ["已送达", funnel.delivered, `最近 1 小时 ${formatCount(funnel.delivered_1h)}`],
  ] as const;
  return (
    <ol aria-label="过去 24 小时漏斗" className="news-funnel-strip">
      {cells.map(([label, value, hint]) => (
        <li key={label}>
          <span className="news-funnel-strip-label">{label}</span>
          <b>{formatCount(value)}</b>
          {hint ? <small>{hint}</small> : null}
        </li>
      ))}
    </ol>
  );
}

function emptyTitle(filters: NewsFeedFilters): string {
  if (filters.outcome === "pushed") return `${hoursLabel(filters.hours)}没有推送`;
  if (filters.outcome === "held") return `${hoursLabel(filters.hours)}没有被拦截的事件`;
  if (filters.outcome === "pending") return "没有正在处理的事件";
  return `${hoursLabel(filters.hours)}没有匹配的事件`;
}

function parseFeedFilters(searchParams: URLSearchParams): NewsFeedFilters {
  return {
    admission: searchParams.get("admission") || null,
    decision: parseDecision(searchParams.get("decision")),
    family: searchParams.get("family") || null,
    hours: parseHours(searchParams.get("hours")),
    outcome: parseOutcome(searchParams.get("outcome")),
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

function parseOutcome(value: string | null): NewsFeedOutcome | null {
  return NEWS_FEED_OUTCOMES.find((candidate) => candidate === value) ?? null;
}

/** `hours` absent → default window; `all` → whole retention; anything else must be a known window. */
function parseHours(value: string | null): number | null {
  if (value == null || value === "") return NEWS_FEED_DEFAULT_HOURS;
  if (value === "all") return null;
  const parsed = Number.parseInt(value, 10);
  return NEWS_FEED_HOURS.includes(parsed) ? parsed : NEWS_FEED_DEFAULT_HOURS;
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
