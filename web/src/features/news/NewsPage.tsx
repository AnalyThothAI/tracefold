import { newsPath, newsStoryPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { ArrowLeft, ChevronDown, ExternalLink, SlidersHorizontal } from "lucide-react";
import { type ReactNode, type RefObject, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import "./news.css";
import "./newsAudit.css";
import "./newsDetail.css";
import "./newsBrief.css";
import "./newsDetailResponsive.css";
import "./newsFeed.css";
import { NewsSourcesRoute, NewsStatusRoute } from "./NewsOperationsPage";
import { NewsSectionTabs } from "./NewsSectionTabs";
import { formatNewsLocalTimestamp } from "./newsTime";
import {
  type BriefPublication,
  type BriefTopStory,
  type NewsBrief,
  type NewsLevel,
  type NewsStatus,
  type NewsStory,
  type NewsStoryDetail,
  type NewsStoryMember,
  useNewsBriefWithToken,
  useNewsFeedHistoryWithToken,
  useNewsFeedWithToken,
  useNewsRealtimeStatusWithToken,
  useNewsStoryWithToken,
} from "./useNewsPage";

type NewsPageProps =
  | { token: string; view: "brief" | "feed" | "sources" | "status" }
  | { storyId: string; token: string; view: "story" };

type FeedMode = "all" | "focus";
type Facet = { count: number; label?: string; value: string };

const DETAIL_TITLE_EXPANSION_THRESHOLD = 56;
const HIGH_SIGNAL_THRESHOLD = 70;

const CATEGORY_LABELS: Record<string, string> = {
  conflict: "冲突",
  protest: "抗议",
  disaster: "灾害",
  diplomatic: "外交",
  economic: "经济",
  terrorism: "恐怖主义",
  cyber: "网络安全",
  health: "公共健康",
  environmental: "环境",
  military: "军事",
  crime: "犯罪",
  infrastructure: "基础设施",
  tech: "科技",
  general: "综合",
};

const LEVEL_LABELS: Record<NewsLevel, string> = {
  critical: "严重",
  high: "高",
  medium: "中",
  low: "低",
  info: "信息",
};

export function NewsPage(props: NewsPageProps) {
  if (props.view === "brief") return <WorldBriefRoute token={props.token} />;
  if (props.view === "sources") return <NewsSourcesRoute token={props.token} />;
  if (props.view === "status") return <NewsStatusRoute token={props.token} />;
  if (props.view === "story") return <StoryRoute storyId={props.storyId} token={props.token} />;
  return <FeedRoute token={props.token} />;
}

function FeedRoute({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const mode: FeedMode = searchParams.get("view") === "focus" ? "focus" : "all";
  const q = searchParams.get("q")?.trim() ?? "";
  const category = searchParams.get("category") || null;
  const level = parseLevel(searchParams.get("level"));
  const reportingOrigin = searchParams.get("reporting_origin") || null;
  const sort = searchParams.get("sort") === "importance" ? "importance" : "latest";
  const providerScoreGt = mode === "focus" ? HIGH_SIGNAL_THRESHOLD : null;
  useEffect(() => {
    if (!searchParams.has("provider_score_gt")) return;
    const canonicalParams = new URLSearchParams(searchParams);
    canonicalParams.delete("provider_score_gt");
    setSearchParams(canonicalParams, { replace: true });
  }, [searchParams, setSearchParams]);
  const query = useNewsFeedWithToken(token, {
    category,
    level,
    providerScoreGt,
    q,
    reportingOrigin,
    sort,
  });
  const statusQuery = useNewsRealtimeStatusWithToken(token);
  const feedIdentity = [mode, q, category ?? "", level ?? "", reportingOrigin ?? "", sort].join(
    "\u001f",
  );
  const [historyAnchor, setHistoryAnchor] = useState<{
    cursor: string | null;
    feedIdentity: string;
  } | null>(null);
  const [historyRequested, setHistoryRequested] = useState(false);
  const firstPage = query.data;
  useEffect(() => {
    if (!firstPage || historyAnchor?.feedIdentity === feedIdentity) return;
    setHistoryAnchor({ cursor: firstPage.next_cursor, feedIdentity });
    setHistoryRequested(false);
  }, [feedIdentity, firstPage, historyAnchor?.feedIdentity]);
  const historyCursor = historyAnchor?.feedIdentity === feedIdentity ? historyAnchor.cursor : null;
  const historyQuery = useNewsFeedHistoryWithToken(
    token,
    {
      category,
      level,
      providerScoreGt,
      q,
      reportingOrigin,
      sort,
    },
    historyCursor,
    historyRequested,
  );
  const pages = historyQuery.data?.pages ?? [];
  const serverStories = Array.from(
    new Map(
      [firstPage?.stories ?? [], ...pages.map((page) => page.stories)]
        .flat()
        .map((story) => [story.story_id, story]),
    ).values(),
  );
  const categoryFacets = firstPage?.facets.categories ?? [];
  const originFacets = firstPage?.facets.reporting_origins ?? [];
  const hasActiveFilters = Boolean(q || category || level || reportingOrigin);
  const storyListRef = useRef<HTMLDivElement>(null);
  const storyFeed = useAnchoredStoryFeed(
    storyListRef,
    serverStories,
    firstPage?.stories ?? [],
    feedIdentity,
  );
  const stories = storyFeed.stories;

  const updateFeedParams = (changes: {
    category?: string | null;
    level?: NewsLevel | null;
    q?: string | null;
    reportingOrigin?: string | null;
    sort?: "importance" | "latest";
  }) => {
    const params = new URLSearchParams(searchParams);
    params.delete("provider_score_gt");
    const nextSort = changes.sort ?? sort;
    params.set("sort", nextSort);
    setOptionalParam(params, "q", changes.q === undefined ? q : changes.q);
    setOptionalParam(
      params,
      "category",
      changes.category === undefined ? category : changes.category,
    );
    setOptionalParam(params, "level", changes.level === undefined ? level : changes.level);
    setOptionalParam(
      params,
      "reporting_origin",
      changes.reportingOrigin === undefined ? reportingOrigin : changes.reportingOrigin,
    );
    setSearchParams(params, { replace: true });
  };

  const resetFilters = () =>
    updateFeedParams({ category: null, level: null, q: null, reportingOrigin: null });

  return (
    <section
      aria-label="全球新闻"
      className="radar-panel news-panel news-story-shell"
      data-feed-density="compact"
      data-page-archetype="scan"
    >
      <header className="news-story-header">
        <div className="news-heading-copy">
          <h1>全球新闻</h1>
          <p>
            {mode === "focus"
              ? `OpenNews > ${HIGH_SIGNAL_THRESHOLD} · 高信号动态`
              : "账户 Strategy 命中的新闻事件"}
          </p>
        </div>
        <NewsInlineStatus
          error={statusQuery.isError}
          fetching={statusQuery.isFetching}
          status={statusQuery.data}
        />
      </header>

      <div className="news-feed-toolbar">
        <NewsSectionTabs active="feed" />
        <NewsFeedControls
          category={category}
          categoryFacets={categoryFacets}
          level={level}
          mode={mode}
          onChange={updateFeedParams}
          originFacets={originFacets}
          reportingOrigin={reportingOrigin}
          searchParams={searchParams}
          sort={sort}
        />
      </div>

      <ActiveFilterChips
        category={category}
        level={level}
        onRemove={updateFeedParams}
        q={q}
        reportingOrigin={reportingOrigin}
      />

      {query.isLoading && !query.data ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取新闻" />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {!query.isLoading && !query.isError && !stories.length ? (
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
              : mode === "focus"
                ? `当前没有 OpenNews 分数严格高于 ${HIGH_SIGNAL_THRESHOLD} 的新闻。`
                : "新闻会在下一轮公共来源采集后出现。"
          }
          title={hasActiveFilters ? "没有匹配的新闻" : "暂无新闻"}
        />
      ) : null}
      {stories.length ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <div className="news-feed-results">
            {storyFeed.count ? (
              <button className="news-new-stories" onClick={storyFeed.reveal} type="button">
                {storyFeed.count} 条新新闻 · 回到顶部
              </button>
            ) : null}
            <div className="news-story-list" ref={storyListRef}>
              {stories.map((story) => (
                <StoryCard key={story.story_id} story={story} />
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
                {historyQuery.isFetchingNextPage ? "正在加载" : "加载更多新闻"}
              </button>
            ) : null}
          </div>
        </PageState.Stale>
      ) : null}
    </section>
  );
}

function NewsFeedControls({
  category,
  categoryFacets,
  level,
  mode,
  onChange,
  originFacets,
  reportingOrigin,
  searchParams,
  sort,
}: {
  category: string | null;
  categoryFacets: Facet[];
  level: NewsLevel | null;
  mode: FeedMode;
  onChange: (changes: {
    category?: string | null;
    level?: NewsLevel | null;
    reportingOrigin?: string | null;
    sort?: "importance" | "latest";
  }) => void;
  originFacets: Facet[];
  reportingOrigin: string | null;
  searchParams: URLSearchParams;
  sort: "importance" | "latest";
}) {
  return (
    <div className="news-filter-bar">
      <NewsFeedModeTabs active={mode} searchParams={searchParams} />
      <label className="news-sort-control">
        <span>排序</span>
        <select
          aria-label="新闻排序"
          onChange={(event) =>
            onChange({ sort: event.target.value === "importance" ? "importance" : "latest" })
          }
          value={sort}
        >
          <option value="latest">最新</option>
          <option value="importance">Tracefold 重要度</option>
        </select>
      </label>
      <details className="news-filter-disclosure">
        <summary>
          <SlidersHorizontal aria-hidden />
          筛选
        </summary>
        <div>
          <label>
            <span>分类</span>
            <select
              aria-label="新闻分类"
              onChange={(event) => onChange({ category: event.target.value || null })}
              value={category ?? ""}
            >
              <option value="">全部分类</option>
              {withSelectedFacet(categoryFacets, category).map((facet) => (
                <option key={facet.value} value={facet.value}>
                  {CATEGORY_LABELS[facet.value] ?? facet.label ?? facet.value} · {facet.count}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>风险</span>
            <select
              aria-label="新闻严重度"
              onChange={(event) => onChange({ level: parseLevel(event.target.value) })}
              value={level ?? ""}
            >
              <option value="">全部级别</option>
              {(Object.keys(LEVEL_LABELS) as NewsLevel[]).map((value) => (
                <option key={value} value={value}>
                  {LEVEL_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>来源</span>
            <select
              aria-label="新闻报道来源"
              onChange={(event) => onChange({ reportingOrigin: event.target.value || null })}
              value={reportingOrigin ?? ""}
            >
              <option value="">全部来源</option>
              {withSelectedFacet(originFacets, reportingOrigin).map((facet) => (
                <option key={facet.value} value={facet.value}>
                  {facet.label ?? facet.value} · {facet.count}
                </option>
              ))}
            </select>
          </label>
        </div>
      </details>
    </div>
  );
}

function NewsFeedModeTabs({
  active,
  searchParams,
}: {
  active: FeedMode;
  searchParams: URLSearchParams;
}) {
  return (
    <nav aria-label="新闻范围" className="news-feed-mode">
      <Link
        aria-current={active === "focus" ? "page" : undefined}
        to={{ pathname: newsPath(), search: feedModeSearch(searchParams, "focus") }}
      >
        <span>重点</span>
        <small>OpenNews &gt; {HIGH_SIGNAL_THRESHOLD}</small>
      </Link>
      <Link
        aria-current={active === "all" ? "page" : undefined}
        to={{ pathname: newsPath(), search: feedModeSearch(searchParams, "all") }}
      >
        全部
      </Link>
    </nav>
  );
}

function ActiveFilterChips({
  category,
  level,
  onRemove,
  q,
  reportingOrigin,
}: {
  category: string | null;
  level: NewsLevel | null;
  onRemove: (changes: {
    category?: string | null;
    level?: NewsLevel | null;
    q?: string | null;
    reportingOrigin?: string | null;
  }) => void;
  q: string;
  reportingOrigin: string | null;
}) {
  const chips = [
    q ? { label: `搜索：${q}`, remove: () => onRemove({ q: null }) } : null,
    category
      ? {
          label: `分类：${CATEGORY_LABELS[category] ?? category}`,
          remove: () => onRemove({ category: null }),
        }
      : null,
    level
      ? { label: `风险：${LEVEL_LABELS[level]}`, remove: () => onRemove({ level: null }) }
      : null,
    reportingOrigin
      ? { label: `来源：${reportingOrigin}`, remove: () => onRemove({ reportingOrigin: null }) }
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
  status?: { realtime: NewsStatus["realtime"] };
}) {
  const readerStatus = readerFacingNewsStatus(error, status);
  const realtime = status?.realtime;
  return (
    <div className="news-inline-status" data-state={readerStatus.state} role="status">
      <span aria-hidden className="news-status-dot" />
      <span>
        <b>{readerStatus.label}</b>
        <small>{realtimeLatencySummary(realtime, fetching)}</small>
      </span>
    </div>
  );
}

function StoryCard({ story }: { story: NewsStory }) {
  const displayTitle = story.title;
  const summary = validSummary(story.description, displayTitle);
  const originalUrl = story.url;
  const providerSignal = providerSignalPresentation(
    story.provider_evidence?.provider_metadata.signal,
  );
  const factors = story.importance_factors;
  const boosts = [
    factors.diplomacy_flashpoint_boost
      ? `外交热点 +${formatPoints(factors.diplomacy_flashpoint_boost)}`
      : "",
    factors.entity_corroboration_boost
      ? `实体佐证 +${formatPoints(factors.entity_corroboration_boost)}`
      : "",
  ].filter(Boolean);

  return (
    <article
      className="news-story-row"
      data-level={story.level}
      data-signal-tone={providerSignal?.tone ?? "none"}
      data-story-id={story.story_id}
    >
      <div className="news-story-primary">
        <header className="news-story-meta">
          <span className="news-story-classification">
            <OpenNewsScoreBadge score={story.provider_evidence?.provider_metadata.score} />
            <ProviderSignalBadge signal={providerSignal} />
            <span className="news-severity" data-level={story.level}>
              {LEVEL_LABELS[story.level]}
            </span>
            <span>{CATEGORY_LABELS[story.category] ?? story.category}</span>
          </span>
          <span className="news-story-context">
            <span>{story.source_name}</span>
            <time dateTime={new Date(story.last_published_at_ms).toISOString()}>
              {displayTime(story.last_published_at_ms)}
            </time>
            <span>{story.source_count} 家独立来源</span>
          </span>
        </header>
        <Link className="news-story-title" to={newsStoryPath(story.story_id)}>
          <h2>{displayTitle}</h2>
        </Link>
        {summary ? <p className="news-story-summary">{summary}</p> : null}
        <footer className="news-story-footer">
          <RelatedAssets assets={story.provider_evidence?.provider_metadata.assets} />
          <details className="news-story-why">
            <summary>
              <span>为什么重要</span>
              <b>Tracefold {story.importance_score}</b>
              <ChevronDown aria-hidden />
            </summary>
            <p>
              严重度 {formatPoints(factors.severity_points)} · 来源{" "}
              {formatPoints(factors.source_points)} · 佐证{" "}
              {formatPoints(factors.corroboration_points)} · 时效{" "}
              {formatPoints(factors.recency_points)}
              {boosts.length ? ` · ${boosts.join(" · ")}` : ""}
            </p>
          </details>
          {originalUrl ? (
            <a className="news-original-link" href={originalUrl} rel="noreferrer" target="_blank">
              查看原文
              <ExternalLink aria-hidden />
            </a>
          ) : null}
        </footer>
      </div>
    </article>
  );
}

function StoryRoute({ token, storyId }: { token: string; storyId: string }) {
  const query = useNewsStoryWithToken(token, storyId);
  const storyPages = query.data?.pages ?? [];
  const firstStoryPage = storyPages[0];
  const story: NewsStoryDetail | undefined = firstStoryPage
    ? {
        ...firstStoryPage,
        members: Array.from(
          new Map(
            storyPages.flatMap((page) => page.members).map((member) => [member.item_id, member]),
          ).values(),
        ),
        members_page: storyPages.at(-1)?.members_page ?? firstStoryPage.members_page,
      }
    : undefined;
  const representativeMember = story?.members.find(
    (member) => member.item_id === story.representative_item_id,
  );
  const scoringMember = story?.members.find((member) => member.item_id === story.scoring_item_id);
  return (
    <section
      aria-label="新闻事件详情"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <NewsSectionTabs active="story" />
      <header className="news-toolbar">
        <Link className="news-back-link" to={newsPath()}>
          <ArrowLeft aria-hidden />
          返回全球新闻
        </Link>
        <span className="news-live-state">{query.isFetching ? "正在刷新" : "证据已保存"}</span>
      </header>
      {query.isLoading && !story ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取新闻详情" />
      ) : null}
      {query.isError && !story ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {story ? (
        <article className="news-story-detail">
          <StoryHero story={story} />

          <section className="news-detail-card news-evidence-section">
            <header>
              <div>
                <span className="news-eyebrow">报道证据</span>
                <h2>{story.source_count} 家独立来源</h2>
              </div>
              <span>{story.item_count} 条报道</span>
            </header>
            <div className="news-member-list">
              {story.members.map((member) => (
                <StoryMemberCard
                  isRepresentative={member.item_id === story.representative_item_id}
                  isScoring={member.item_id === story.scoring_item_id}
                  key={member.item_id}
                  member={member}
                />
              ))}
            </div>
            {query.hasNextPage ? (
              <button
                className="news-members-load-more"
                disabled={query.isFetchingNextPage}
                onClick={() => void query.fetchNextPage()}
                type="button"
              >
                {query.isFetchingNextPage ? "正在加载相关报道" : "加载更多相关报道"}
              </button>
            ) : null}
          </section>

          <details className="news-audit-disclosure">
            <summary>查看 Tracefold 评分与新闻事件审计</summary>
            <div className="news-audit-content">
              <section>
                <div className="news-audit-heading">
                  <div>
                    <span className="news-eyebrow">确定性排序</span>
                    <h2>Tracefold 重要度 {story.importance_score}</h2>
                  </div>
                </div>
                <p>以下因子由服务端确定性计算，浏览器不重算。</p>
                <ImportanceFactorGrid story={story} scoringSource={scoringMember?.source_name} />
              </section>
              <section>
                <h2>聚合身份</h2>
                <dl className="news-story-identity-grid">
                  <div>
                    <dt>展示代表</dt>
                    <dd>{representativeMember?.source_name ?? story.source_name}</dd>
                    <code>{story.representative_item_id}</code>
                  </div>
                  <div>
                    <dt>评分依据</dt>
                    <dd>{scoringMember?.source_name ?? "评分报道"}</dd>
                    <code>{story.scoring_item_id}</code>
                    {scoringMember?.url ? (
                      <a
                        className="news-audit-source-link"
                        href={scoringMember.url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        评分报道原文
                        <ExternalLink aria-hidden />
                      </a>
                    ) : null}
                  </div>
                </dl>
              </section>
              <section>
                <h2>Story V2 身份证据</h2>
                <p>词法相似度只使用 Jaccard；强事实用于确认或否决，不形成第二套聚类。</p>
                <dl className="news-story-evidence-grid">
                  <div>
                    <dt>固定锚点</dt>
                    <dd>{story.identity_evidence.anchor_item_id}</dd>
                  </div>
                  <div>
                    <dt>强实体</dt>
                    <dd>{auditEvidenceList(story.identity_evidence.strong_entity_keys)}</dd>
                  </div>
                  <div>
                    <dt>动作</dt>
                    <dd>{auditEvidenceList(story.identity_evidence.action_keys)}</dd>
                  </div>
                  <div>
                    <dt>数值 / 地点</dt>
                    <dd>
                      {auditEvidenceList([
                        ...story.identity_evidence.numeric_keys,
                        ...story.identity_evidence.location_keys,
                      ])}
                    </dd>
                  </div>
                  <div>
                    <dt>成员理由</dt>
                    <dd>{auditReasonList(story.identity_evidence.membership_reasons)}</dd>
                  </div>
                  <div>
                    <dt>冲突否决</dt>
                    <dd>{auditReasonList(story.identity_evidence.rejection_reasons)}</dd>
                  </div>
                  <div>
                    <dt>Jaccard 诊断</dt>
                    <dd>
                      接受 {story.identity_evidence.max_accepted_jaccard.toFixed(3)} · 拒绝{" "}
                      {story.identity_evidence.max_rejected_jaccard.toFixed(3)}
                    </dd>
                  </div>
                  <div>
                    <dt>版本</dt>
                    <dd>{story.identity_evidence.identity_version}</dd>
                  </div>
                </dl>
              </section>
            </div>
          </details>
        </article>
      ) : null}
    </section>
  );
}

function auditEvidenceList(values: readonly string[]): string {
  return values.length > 0 ? values.join("、") : "无强事实";
}

function auditReasonList(values: Readonly<Record<string, number>>): string {
  const entries = Object.entries(values);
  return entries.length > 0
    ? entries.map(([reason, count]) => `${reason} × ${count}`).join("、")
    : "无";
}

function StoryHero({ story }: { story: NewsStory }) {
  const displayTitle = story.title;
  const summary = validSummary(story.description, displayTitle);
  const originalUrl = story.url;
  const titleRef = useRef<HTMLHeadingElement>(null);
  const displayTitleOverflows = useTitleOverflow(titleRef, displayTitle);
  const displayTitleIsLong =
    displayTitle.length > DETAIL_TITLE_EXPANSION_THRESHOLD || displayTitleOverflows;
  return (
    <header className="news-story-hero">
      <div className="news-story-badges">
        <OpenNewsScoreBadge score={story.provider_evidence?.provider_metadata.score} />
        <ProviderSignalBadge
          signal={providerSignalPresentation(story.provider_evidence?.provider_metadata.signal)}
        />
        <span data-level={story.level}>{LEVEL_LABELS[story.level]}</span>
        <span>{CATEGORY_LABELS[story.category] ?? story.category}</span>
        <span>{story.source_name}</span>
      </div>
      <h1 className="is-clamped" ref={titleRef}>
        {displayTitle}
      </h1>
      {displayTitleIsLong ? (
        <details className="news-title-expansion">
          <summary>
            <span className="news-title-expand-label">展开完整标题</span>
            <span className="news-title-collapse-label">收起完整标题</span>
          </summary>
          {displayTitleIsLong ? <p>{displayTitle}</p> : null}
        </details>
      ) : null}
      {story.original_title !== displayTitle ? (
        <p className="news-original-title">
          <span>原标题</span>
          {story.original_title}
        </p>
      ) : null}
      {summary ? <p className="news-story-lead">{summary}</p> : null}
      <div className="news-story-support">
        <RelatedAssets assets={story.provider_evidence?.provider_metadata.assets} />
      </div>
      <div className="news-story-hero-footer">
        <div className="news-evidence-metrics">
          <span>
            <b>{story.source_count}</b>独立来源
          </span>
          <span>
            <b>{story.item_count}</b>相关报道
          </span>
          <span>
            <time dateTime={new Date(story.last_published_at_ms).toISOString()}>
              <b>{displayTime(story.last_published_at_ms)}</b>
            </time>
            最后报道
          </span>
        </div>
        {originalUrl ? (
          <a className="news-primary-link" href={originalUrl} rel="noreferrer" target="_blank">
            阅读代表原文
            <ExternalLink aria-hidden />
          </a>
        ) : null}
      </div>
    </header>
  );
}

function OpenNewsScoreBadge({ score }: { score: number | null | undefined }) {
  const numericScore = numericProviderScore(score);
  if (numericScore == null) return null;
  const formattedScore = formatPoints(numericScore);
  return (
    <span
      aria-label={`OpenNews 评分 ${formattedScore}`}
      className="news-provider-score"
      data-band={numericScore > HIGH_SIGNAL_THRESHOLD ? "high" : "standard"}
    >
      <span>OpenNews</span>
      <b>{formattedScore}</b>
    </span>
  );
}

type ProviderSignalPresentation = {
  label: string;
  tone: "info" | "negative" | "neutral" | "positive";
};

function ProviderSignalBadge({ signal }: { signal: ProviderSignalPresentation | null }) {
  if (!signal) return null;
  return (
    <span
      aria-label={`OpenNews 信号 ${signal.label}`}
      className="news-provider-signal"
      data-tone={signal.tone}
    >
      <span>信号</span>
      <b>{signal.label}</b>
    </span>
  );
}

function providerSignalPresentation(
  signal: string | null | undefined,
): ProviderSignalPresentation | null {
  const value = signal?.trim();
  if (!value) return null;
  const normalized = value.toLowerCase();
  if (["bullish", "buy", "long", "positive", "up"].includes(normalized)) {
    return { label: "利多", tone: "positive" };
  }
  if (["bearish", "down", "negative", "sell", "short"].includes(normalized)) {
    return { label: "利空", tone: "negative" };
  }
  if (["flat", "hold", "neutral"].includes(normalized)) {
    return { label: "中性", tone: "neutral" };
  }
  return { label: value, tone: "info" };
}

function RelatedAssets({ assets }: { assets: readonly { symbol: string }[] | null | undefined }) {
  return (
    <div aria-label="关联资产" className="news-related-assets" role="group">
      <span>关联资产</span>
      {assets?.length ? (
        <ul aria-label="关联资产">
          {assets.map((asset, index) => (
            <li key={`${asset.symbol}:${index}`}>{asset.symbol}</li>
          ))}
        </ul>
      ) : (
        <span className="news-related-assets-empty">上游未标注</span>
      )}
    </div>
  );
}

function StoryMemberCard({
  isRepresentative,
  isScoring,
  member,
}: {
  isRepresentative: boolean;
  isScoring: boolean;
  member: NewsStoryMember;
}) {
  const title = normalizeMemberEvidence(member.title) || "未命名报道";
  const summary = validMemberSummary(member.description, title);
  return (
    <article className="news-member-card">
      <header>
        <b>{member.reporting_origin}</b>
        <time dateTime={new Date(member.published_at_ms).toISOString()}>
          {absoluteTime(member.published_at_ms)}
        </time>
        {isRepresentative ? <span>展示代表</span> : null}
        {isScoring ? <span>评分依据</span> : null}
      </header>
      <h3>{title}</h3>
      {member.original_title !== member.title ? (
        <p className="news-member-original-title">
          <span>原标题</span>
          {normalizeMemberEvidence(member.original_title)}
        </p>
      ) : null}
      {summary ? <p>{summary}</p> : null}
      <div className="news-member-actions">
        {member.url ? (
          <a href={member.url} rel="noreferrer" target="_blank">
            阅读原文
            <ExternalLink aria-hidden />
          </a>
        ) : null}
      </div>
    </article>
  );
}

function ImportanceFactorGrid({
  scoringSource,
  story,
}: {
  scoringSource?: string;
  story: NewsStory;
}) {
  const factors = story.importance_factors;
  return (
    <dl className="news-factor-grid">
      <div>
        <dt>严重度</dt>
        <dd>{formatPoints(factors.severity_points)}</dd>
        <small>{LEVEL_LABELS[factors.severity_level]}</small>
      </div>
      <div>
        <dt>来源质量</dt>
        <dd>{formatPoints(factors.source_points)}</dd>
        <small>
          {scoringSource ?? "评分报道"} · Tier {factors.source_tier}
        </small>
      </div>
      <div>
        <dt>佐证</dt>
        <dd>{formatPoints(factors.corroboration_points)}</dd>
        <small>{factors.scoring_corroboration_count} 个计分来源</small>
      </div>
      <div>
        <dt>时效</dt>
        <dd>{formatPoints(factors.recency_points)}</dd>
      </div>
      <div>
        <dt>外交热点</dt>
        <dd>+{formatPoints(factors.diplomacy_flashpoint_boost)}</dd>
      </div>
      <div>
        <dt>实体佐证</dt>
        <dd>+{formatPoints(factors.entity_corroboration_boost)}</dd>
      </div>
      <div>
        <dt>报道来源</dt>
        <dd>{factors.reporting_origin_count}</dd>
      </div>
      <div>
        <dt>总重要度</dt>
        <dd>{factors.total}</dd>
      </div>
    </dl>
  );
}

function WorldBriefRoute({ token }: { token: string }) {
  const query = useNewsBriefWithToken(token);
  const brief = query.data;
  return (
    <section
      aria-label="公共全球简报"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <NewsSectionTabs active="brief" />
      <header className="news-toolbar news-brief-toolbar">
        <div>
          <span className="news-eyebrow">PUBLIC WORLD BRIEF</span>
          <h1>公共全球简报</h1>
          <p>服务端确定性精选，AI 仅作为独立增强层。</p>
        </div>
        <span className="news-brief-state" data-state={brief?.state ?? "unavailable"}>
          {briefStateLabel(brief?.state)}
        </span>
      </header>
      {query.isLoading && !brief ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取公共全球简报" />
      ) : null}
      {query.isError && !brief ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {brief && !brief.publication ? (
        <PageState.Empty
          hint={briefUnavailableHint(brief.next_due_at_ms)}
          title="尚无公共全球简报"
        />
      ) : null}
      {brief?.publication ? (
        <BriefDocument publication={brief.publication} state={brief.state} />
      ) : null}
    </section>
  );
}

function BriefDocument({
  publication,
  state,
}: {
  publication: BriefPublication;
  state: NewsBrief["state"];
}) {
  const selectionStats = publication.provenance.selection_stats;
  return (
    <article className="news-brief-document">
      <header className="news-brief-publication-meta">
        <span>{state === "last_known_good" ? "完整快照 · 保留展示" : "原子发布快照"}</span>
        <p>发布 {absoluteTime(publication.published_at_ms)}</p>
        <p>
          来源时间 {absoluteTime(publication.source_age_range.oldest_ms)} 至{" "}
          {absoluteTime(publication.source_age_range.newest_ms)}
        </p>
        <ul aria-label="公开精选漏斗" className="news-brief-selection-stats">
          <li>候选 {selectionStats.considered}</li>
          <li>可作主线 {selectionStats.brief_eligible_considered}</li>
          <li>主线补位 {selectionStats.brief_eligible_promoted ? "已触发" : "未触发"}</li>
          <li>准入剔除 {selectionStats.admissibility_dropped}</li>
          <li>来源上限剔除 {selectionStats.source_cap_dropped}</li>
          <li>名额溢出剔除 {selectionStats.overflow_dropped}</li>
        </ul>
      </header>
      <section aria-labelledby="news-brief-top-stories" className="news-brief-top-stories">
        <header>
          <div>
            <span className="news-eyebrow">SERVER-RANKED</span>
            <h2 id="news-brief-top-stories">公开重点新闻</h2>
          </div>
          <span>{publication.top_stories.length} 个新闻事件 · 严格按服务器顺序</span>
        </header>
        <ol>
          {publication.top_stories.map((story, index) => (
            <BriefTopStoryCard index={index + 1} key={`${story.story_id}:${index}`} story={story} />
          ))}
        </ol>
      </section>
      <BriefEnhancement publication={publication} />
    </article>
  );
}

function BriefTopStoryCard({ index, story }: { index: number; story: BriefTopStory }) {
  const primaryLink = validExternalUrl(story.primary_link);
  return (
    <li>
      <article className="news-brief-story" data-testid="brief-top-story">
        <header>
          <span className="news-brief-rank">#{index}</span>
          <span data-threat={story.threat_level}>{publicThreatLabel(story.threat_level)}</span>
          <span>{publicCategoryLabel(story.category)}</span>
        </header>
        <h2>{story.primary_title}</h2>
        <div className="news-brief-story-meta">
          <b>{story.primary_source}</b>
          <time dateTime={new Date(story.primary_published_at_ms).toISOString()}>
            主要来源 {displayTime(story.primary_published_at_ms)}
          </time>
          <span>来源更新 {displayTime(story.last_updated_ms)}</span>
          <span>{story.source_count} 条报道</span>
          <span>{story.unique_source_count} 家独立来源</span>
        </div>
        <p className="news-brief-sources">{story.sources.join("、")}</p>
        <div className="news-brief-story-actions">
          <Link to={newsStoryPath(story.story_id)}>查看新闻事件</Link>
          {primaryLink ? (
            <a href={primaryLink} rel="noreferrer" target="_blank">
              阅读主要来源
              <ExternalLink aria-hidden />
            </a>
          ) : (
            <span>无主要来源链接</span>
          )}
        </div>
        <details className="news-brief-members">
          <summary>相关标题 · {story.member_titles.length}</summary>
          <ul>
            {story.member_titles.map((title, memberIndex) => (
              <li key={`${story.story_id}:${memberIndex}`}>{title}</li>
            ))}
          </ul>
        </details>
      </article>
    </li>
  );
}

function BriefEnhancement({ publication }: { publication: BriefPublication }) {
  if (publication.brief_kind === "none") {
    return (
      <section className="news-brief-no-enhancement">
        <span>本版没有 AI 增强</span>
      </section>
    );
  }
  const kindLabel = publication.brief_kind === "l1" ? "L1 · 完整校验" : "L2 · 降级概览";
  return (
    <section aria-labelledby="news-brief-enhancement" className="news-brief-enhancement">
      <header>
        <div>
          <span className="news-eyebrow">AI ENHANCEMENT</span>
          <h2 id="news-brief-enhancement">AI 增强概览</h2>
        </div>
        <span>{kindLabel}</span>
      </header>
      <p className="news-brief-world-brief">
        {renderBriefCitations(publication.world_brief, publication.top_stories)}
      </p>
      {publication.brief_kind === "l1" && publication.brief_story_lines.length ? (
        <ol aria-label="AI 新闻事件摘要" className="news-brief-lines">
          {publication.brief_story_lines.map((line) => {
            const story = publication.top_stories[line.n - 1];
            return (
              <li key={line.n}>
                <p>{renderBriefCitations(line.text, publication.top_stories)}</p>
                {story ? <Link to={newsStoryPath(story.story_id)}>查看新闻事件</Link> : null}
              </li>
            );
          })}
        </ol>
      ) : null}
      <footer>
        {publication.provider}/{publication.model}
      </footer>
    </section>
  );
}

function renderBriefCitations(text: string, topStories: readonly BriefTopStory[]): ReactNode[] {
  const fragments: ReactNode[] = [];
  let cursor = 0;
  for (const match of text.matchAll(/\[(\d{1,3})\]/g)) {
    const marker = match[0];
    const start = match.index;
    if (start > cursor) fragments.push(text.slice(cursor, start));
    const citationNumber = Number.parseInt(match[1] ?? "0", 10);
    const story = topStories[citationNumber - 1];
    fragments.push(
      story ? (
        <Link
          aria-label={`引用 ${citationNumber}：${story.primary_title}`}
          className="news-brief-citation"
          key={`${start}:${marker}`}
          to={newsStoryPath(story.story_id)}
        >
          {marker}
        </Link>
      ) : (
        marker
      ),
    );
    cursor = start + marker.length;
  }
  if (cursor < text.length) fragments.push(text.slice(cursor));
  return fragments;
}

function parseLevel(value: string | null): NewsLevel | null {
  return value && value in LEVEL_LABELS ? (value as NewsLevel) : null;
}

function setOptionalParam(params: URLSearchParams, name: string, value: string | null | undefined) {
  if (value) params.set(name, value);
  else params.delete(name);
}

function feedModeSearch(searchParams: URLSearchParams, mode: FeedMode): string {
  const params = new URLSearchParams(searchParams);
  params.delete("provider_score_gt");
  if (mode === "focus") params.set("view", "focus");
  else params.delete("view");
  const value = params.toString();
  return value ? `?${value}` : "";
}

function numericProviderScore(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function validExternalUrl(value: string | null | undefined): string | null {
  const normalized = value?.trim() ?? "";
  return /^https?:\/\//i.test(normalized) ? normalized : null;
}

function withSelectedFacet(facets: Facet[], selected: string | null): Facet[] {
  if (!selected || facets.some((facet) => facet.value === selected)) return facets;
  return [{ count: 0, label: selected, value: selected }, ...facets];
}

function validSummary(value: string | null | undefined, title: string): string | null {
  const summary = value?.trim() ?? "";
  if (!summary || summary === title.trim() || summary.length < 8) return null;
  if (/^(?:n\/?a|none|null|undefined|no description)$/i.test(summary)) return null;
  return summary;
}

function normalizeMemberEvidence(value: string | null | undefined): string {
  if (!value) return "";
  const withBreakSpacing = value.replace(/<\/?(?:br|dd|div|dt|hr|li|p|td|th|tr)\b[^>]*>/gi, " ");
  const document = new DOMParser().parseFromString(withBreakSpacing, "text/html");
  const plainText = (document.body.textContent ?? "").normalize("NFC");
  return plainText
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")
    .replace(/https?:\/\/[^\s<>]+/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function validMemberSummary(value: string | null | undefined, title: string): string | null {
  const summary = normalizeMemberEvidence(value);
  if (!summary || summary === title || summary.length < 8) return null;
  if (/^(?:n\/?a|none|null|undefined|no description)$/i.test(summary)) return null;
  return summary;
}

function useAnchoredStoryFeed(
  listRef: RefObject<HTMLDivElement | null>,
  serverStories: NewsStory[],
  firstPageStories: NewsStory[],
  identity: string,
) {
  const [count, setCount] = useState(0);
  const [, setRevision] = useState(0);
  const acceptedStoriesRef = useRef<NewsStory[]>(serverStories);
  const awayFromTopRef = useRef(false);
  const deferredTopIdsRef = useRef<Set<string>>(new Set());
  const deferredRef = useRef(false);
  const identityRef = useRef<string | null>(null);
  const knownIdsRef = useRef<Set<string>>(new Set());
  const latestStoriesRef = useRef<NewsStory[]>(serverStories);
  const scrollContainerRef = useRef<HTMLElement | null>(null);
  const firstPageKey = firstPageStories.map((story) => story.story_id).join("\u001f");
  latestStoriesRef.current = serverStories;
  const identityChanged = identityRef.current !== identity;
  const addedTopIds = identityChanged
    ? []
    : firstPageStories.flatMap((story) =>
        knownIdsRef.current.has(story.story_id) ? [] : [story.story_id],
      );
  const startsDeferral = addedTopIds.length > 0 && awayFromTopRef.current;
  const shouldDefer = deferredRef.current || startsDeferral;
  const excludedTopIds = startsDeferral
    ? new Set([...deferredTopIdsRef.current, ...addedTopIds])
    : deferredTopIdsRef.current;
  const stories = shouldDefer
    ? appendNonDeferredTail(acceptedStoriesRef.current, serverStories, excludedTopIds)
    : serverStories;

  useEffect(() => {
    const scrollContainer =
      listRef.current?.closest<HTMLElement>(".center-column") ?? document.documentElement;
    scrollContainerRef.current = scrollContainer;
    const handleScroll = () => {
      awayFromTopRef.current = scrollContainer.scrollTop > 96;
      if (!awayFromTopRef.current && deferredRef.current) {
        acceptedStoriesRef.current = latestStoriesRef.current;
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
    const currentIds = new Set(firstPageStories.map((story) => story.story_id));
    if (identityRef.current !== identity) {
      identityRef.current = identity;
      knownIdsRef.current = currentIds;
      acceptedStoriesRef.current = serverStories;
      deferredTopIdsRef.current.clear();
      deferredRef.current = false;
      setCount(0);
      return;
    }
    const newlyAddedIds = firstPageStories.flatMap((story) =>
      knownIdsRef.current.has(story.story_id) ? [] : [story.story_id],
    );
    knownIdsRef.current = currentIds;
    if (newlyAddedIds.length && awayFromTopRef.current) {
      let newlyDeferred = 0;
      for (const storyId of newlyAddedIds) {
        if (deferredTopIdsRef.current.has(storyId)) continue;
        deferredTopIdsRef.current.add(storyId);
        newlyDeferred += 1;
      }
      deferredRef.current = true;
      if (newlyDeferred) setCount((current) => current + newlyDeferred);
      return;
    }
    if (!deferredRef.current) acceptedStoriesRef.current = serverStories;
  }, [firstPageKey, firstPageStories, identity, serverStories]);

  const reveal = () => {
    const scrollContainer = scrollContainerRef.current;
    acceptedStoriesRef.current = latestStoriesRef.current;
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

  return { count, reveal, stories };
}

function appendNonDeferredTail(
  acceptedStories: NewsStory[],
  serverStories: NewsStory[],
  deferredTopIds: Set<string>,
): NewsStory[] {
  const seen = new Set(acceptedStories.map((story) => story.story_id));
  const appended = serverStories.filter((story) => {
    if (deferredTopIds.has(story.story_id) || seen.has(story.story_id)) return false;
    seen.add(story.story_id);
    return true;
  });
  return appended.length ? [...acceptedStories, ...appended] : acceptedStories;
}

function readerFacingNewsStatus(
  error: boolean,
  status: { realtime: NewsStatus["realtime"] } | undefined,
): { label: string; state: "live" | "recovering" | "stalled" } {
  if (error) return { label: "状态暂不可用", state: "stalled" };
  if (!status) return { label: "正在检查 WSS", state: "recovering" };
  if (status.realtime.wss_state === "connected") {
    return { label: "WSS 已连接", state: "live" };
  }
  if (status.realtime.wss_state === "reconnecting") {
    return { label: "WSS 重连中", state: "recovering" };
  }
  return { label: "WSS 不可用", state: "stalled" };
}

function useTitleOverflow(ref: RefObject<HTMLElement | null>, value: string): boolean {
  const [overflowing, setOverflowing] = useState(false);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    const measure = () => {
      if (element.clientHeight === 0) return;
      setOverflowing(element.scrollHeight > element.clientHeight + 1);
    };
    measure();

    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, value]);

  return overflowing;
}

function briefStateLabel(state?: NewsBrief["state"]): string {
  if (state === "current") return "当前公开快报";
  if (state === "degraded") return "当前快报 · AI 增强降级";
  if (state === "last_known_good") return "上一份完整公开快报";
  return "不可用";
}

function briefUnavailableHint(pendingDueAtMs: number | null): string {
  return pendingDueAtMs == null
    ? "等待服务端发布首份完整快照。"
    : `服务端预计在 ${absoluteTime(pendingDueAtMs)} 后再次评估。`;
}

function publicThreatLabel(value: BriefTopStory["threat_level"]): string {
  const labels: Record<BriefTopStory["threat_level"], string> = {
    critical: "严重",
    elevated: "升高",
    high: "高",
    moderate: "中等",
  };
  return labels[value];
}

function publicCategoryLabel(value: BriefTopStory["category"]): string {
  const labels: Record<BriefTopStory["category"], string> = {
    conflict: "冲突",
    crisis: "危机",
    economic: "经济",
    general: "综合",
    geopolitical: "地缘政治",
    natural_disaster: "自然灾害",
    political: "政治",
    unrest: "动荡",
    violence: "暴力",
  };
  return labels[value];
}

function relativeTime(value: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() - value) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} 小时前` : `${Math.floor(hours / 24)} 天前`;
}

function displayTime(value: number): string {
  return `${absoluteTime(value)} · ${relativeTime(value)}`;
}

function realtimeLatencySummary(
  realtime: NewsStatus["realtime"] | undefined,
  fetching: boolean,
): string {
  if (!realtime) return fetching ? "正在检查" : "等待首次状态";
  const prefix = fetching ? "刷新中 · " : "";
  return `${prefix}入站 P50/P95 ${optionalDuration(realtime.inbound_latency.p50_ms)}/${optionalDuration(realtime.inbound_latency.p95_ms)} · Story P50/P95 ${optionalDuration(realtime.story_visible_latency.p50_ms)}/${optionalDuration(realtime.story_visible_latency.p95_ms)}`;
}

function optionalDuration(value: number | null): string {
  if (value == null) return "尚无样本";
  return value < 1_000 ? `${value} ms` : `${(value / 1_000).toFixed(1)} s`;
}

function absoluteTime(value: number): string {
  return formatNewsLocalTimestamp(value);
}

function formatPoints(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
