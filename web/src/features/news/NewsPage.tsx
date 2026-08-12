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
import {
  type BriefPublication,
  type BriefTopStory,
  type NewsBrief,
  type NewsLevel,
  type NewsNotification,
  type NewsStatus,
  type NewsStory,
  type NewsStoryDetail,
  type NewsStoryMember,
  useNewsBriefWithToken,
  useNewsFeedWithToken,
  useNewsStatusWithToken,
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
  const mode: FeedMode = searchParams.get("view") === "all" ? "all" : "focus";
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
  const statusQuery = useNewsStatusWithToken(token);
  const pages = query.data?.pages ?? [];
  const serverStories = Array.from(
    new Map(pages.flatMap((page) => page.stories).map((story) => [story.story_id, story])).values(),
  );
  const firstPage = pages[0];
  const categoryFacets = firstPage?.facets.categories ?? [];
  const originFacets = firstPage?.facets.reporting_origins ?? [];
  const hasActiveFilters = Boolean(q || category || level || reportingOrigin);
  const storyListRef = useRef<HTMLDivElement>(null);
  const feedIdentity = [mode, q, category ?? "", level ?? "", reportingOrigin ?? "", sort].join(
    "\u001f",
  );
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
              : "公共来源完整新闻事件"}
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
            {query.hasNextPage ? (
              <button
                className="news-load-more"
                disabled={query.isFetchingNextPage}
                onClick={() => void query.fetchNextPage()}
                type="button"
              >
                {query.isFetchingNextPage ? "正在加载" : "加载更多新闻"}
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
  status?: NewsStatus;
}) {
  const source = status?.layers.ingest.opennews ?? null;
  const rss = status?.layers.ingest.rss ?? null;
  const readerStatus = readerFacingNewsStatus(error, status);
  const lastSuccessAt = readerLastSuccessAt(status);
  const hasIngestWarning = status?.layers.ingest.status === "degraded";
  return (
    <details className="news-inline-status" data-state={readerStatus.state}>
      <summary>
        <span aria-hidden className="news-status-dot" />
        <span>
          <b>{readerStatus.label}</b>
          <small>
            {fetching
              ? "正在检查"
              : lastSuccessAt != null
                ? `最近同步 ${relativeTime(lastSuccessAt)}`
                : "等待首次同步"}
          </small>
        </span>
      </summary>
      <div className="news-status-diagnostics">
        {source ? (
          <section>
            <h2>OpenNews 低延迟主采集链</h2>
            <dl>
              <div>
                <dt>实时连接</dt>
                <dd>{source.live_connected ? "正常" : "正在恢复"}</dd>
              </div>
              <div>
                <dt>最近补齐</dt>
                <dd>
                  {source.last_recovery_at_ms == null
                    ? "等待首次补齐"
                    : relativeTime(source.last_recovery_at_ms)}
                </dd>
              </div>
              <div>
                <dt>连续失败</dt>
                <dd>{source.consecutive_failures}</dd>
              </div>
            </dl>
          </section>
        ) : null}
        {rss ? (
          <section>
            <h2>公共 RSS 覆盖与交叉印证</h2>
            {rss.enabled ? (
              <dl>
                <div>
                  <dt>已成功来源</dt>
                  <dd>
                    {rss.successful_source_count}/{rss.source_count}
                  </dd>
                </div>
                <div>
                  <dt>当前失败</dt>
                  <dd>{rss.failed_source_count}</dd>
                </div>
                <div>
                  <dt>正在采集</dt>
                  <dd>{rss.claimed_source_count}</dd>
                </div>
              </dl>
            ) : (
              <p>未启用</p>
            )}
          </section>
        ) : null}
        {error || source?.last_error || hasIngestWarning || status?.layers.story.reasons.length ? (
          <p className="news-status-warning">
            数据更新遇到异常，系统正在自动恢复。必要时请通过 CLI 查看诊断代码。
          </p>
        ) : null}
      </div>
    </details>
  );
}

function StoryCard({ story }: { story: NewsStory }) {
  const displayTitle = story.title;
  const summary = validSummary(story.description, displayTitle);
  const originalUrl = story.url;
  const providerSignal = providerSignalPresentation(
    story.provider_evidence?.provider_metadata.signal,
  );
  const notification = story.notification;
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
              {relativeTime(story.last_published_at_ms)}
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
          <NotificationState notification={notification} />
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
            </div>
          </details>
        </article>
      ) : null}
    </section>
  );
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
      {summary ? <p className="news-story-lead">{summary}</p> : null}
      <div className="news-story-support">
        <RelatedAssets assets={story.provider_evidence?.provider_metadata.assets} />
        <NotificationState notification={story.notification} />
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
            <b>{relativeTime(story.last_published_at_ms)}</b>最后报道
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

type NotificationPresentation = {
  detail: string | null;
  label: string;
  tone: "caution" | "info" | "negative" | "neutral" | "success";
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

function NotificationState({ notification }: { notification: NewsNotification }) {
  const presentation = notificationPresentation(notification);
  const accessibleLabel = `通知状态 ${presentation.label}${
    presentation.detail ? `；${presentation.detail}` : ""
  }`;
  return (
    <span
      aria-label={accessibleLabel}
      className="news-notification-state"
      data-tone={presentation.tone}
    >
      <b>{presentation.label}</b>
      {presentation.detail ? (
        <span className="news-notification-detail">· {presentation.detail}</span>
      ) : null}
    </span>
  );
}

function notificationPresentation(notification: NewsNotification): NotificationPresentation {
  const currentDetail = notification.eligible
    ? null
    : notificationIneligibleDetail(notification.ineligible_reason);
  switch (notification.delivery_state) {
    case "sent":
      return {
        detail: currentDetail ? `当前${currentDetail}` : null,
        label: "已通知",
        tone: "success",
      };
    case "pending":
      return {
        detail: currentDetail ? `当前${currentDetail}` : null,
        label: "通知中",
        tone: "info",
      };
    case "failed":
      return {
        detail: currentDetail ? `当前${currentDetail}` : null,
        label: "通知失败",
        tone: "negative",
      };
    case "suppressed":
      return {
        detail: currentDetail ? `当前${currentDetail}` : null,
        label: "已抑制",
        tone: "caution",
      };
    case "not_created":
      break;
  }
  if (notification.eligible) return { detail: null, label: "待通知", tone: "info" };
  return { detail: currentDetail, label: "不通知", tone: "neutral" };
}

function notificationIneligibleDetail(
  reason: NewsNotification["ineligible_reason"],
): string | null {
  if (!reason) return null;
  const detailByReason: Record<NonNullable<NewsNotification["ineligible_reason"]>, string> = {
    baseline: "基线前",
    cl_family_only: "仅 CL 资产",
    disabled: "通知关闭",
    no_asset: "无关联资产",
    score_threshold: "分数未达阈值",
    stale: "已过通知时限",
  };
  return detailByReason[reason];
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
            主要来源 {relativeTime(story.primary_published_at_ms)}
          </time>
          <span>来源更新 {relativeTime(story.last_updated_ms)}</span>
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
  if (mode === "all") params.set("view", "all");
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
  status: NewsStatus | undefined,
): { label: string; state: "live" | "recovering" | "stalled" } {
  if (error) return { label: "状态暂不可用", state: "stalled" };
  if (!status) return { label: "正在检查新闻", state: "recovering" };
  if (status.layers.story.status === "degraded") {
    return { label: "新闻更新异常", state: "stalled" };
  }
  if (status.layers.ingest.status === "degraded") {
    return { label: "新闻更新异常", state: "stalled" };
  }
  const factStates = [status.layers.ingest.status, status.layers.story.status];
  if (factStates.includes("warming")) {
    return { label: "新闻数据恢复中", state: "recovering" };
  }
  return { label: "新闻已同步", state: "live" };
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

function readerLastSuccessAt(status?: NewsStatus): number | null {
  if (!status) return null;
  const candidates = [
    status.layers.ingest.opennews?.last_success_at_ms,
    status.layers.story.last_success_at_ms,
  ].filter((value): value is number => value != null);
  return candidates.length ? Math.min(...candidates) : null;
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

function absoluteTime(value: number): string {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function formatPoints(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
