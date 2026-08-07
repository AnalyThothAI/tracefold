import { newsBriefPath, newsPath, newsStoryPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { ArrowLeft, ExternalLink, SlidersHorizontal } from "lucide-react";
import { type RefObject, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import "./news.css";
import "./newsAudit.css";
import "./newsDetail.css";
import "./newsBrief.css";
import "./newsDetailResponsive.css";
import "./newsFeed.css";
import {
  type BriefPublication,
  type NewsFeed,
  type NewsLevel,
  type NewsProviderCoin,
  type NewsProviderMetadata,
  type NewsPushDeliveryState,
  type NewsStatus,
  type NewsStory,
  type NewsStoryDetail,
  type NewsStoryMember,
  type NewsTitleTranslation,
  type NewsTranslationLayer,
  useNewsBriefWithToken,
  useNewsFeedWithToken,
  useNewsStatusWithToken,
  useNewsStoryWithToken,
} from "./useNewsPage";

type NewsPageProps = {
  brief?: boolean;
  storyId?: string | null;
  token: string;
};

type FeedView = "focus" | "all";
type Facet = { count: number; label?: string; value: string };

const HIGH_SIGNAL_THRESHOLD = 70;
const DETAIL_TITLE_EXPANSION_THRESHOLD = 56;

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

const PUSH_DELIVERY_LABELS: Record<NewsPushDeliveryState, string> = {
  pending: "推送处理中",
  sent: "已推送",
  suppressed: "未推送",
  failed: "推送失败",
};

export function NewsPage({ brief = false, token, storyId = null }: NewsPageProps) {
  if (brief) return <WorldBriefRoute token={token} />;
  if (storyId) return <StoryRoute storyId={storyId} token={token} />;
  return <FeedRoute token={token} />;
}

function FeedRoute({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const view: FeedView = searchParams.get("view") === "all" ? "all" : "focus";
  const q = searchParams.get("q")?.trim() ?? "";
  const category = searchParams.get("category") || null;
  const level = parseLevel(searchParams.get("level"));
  const reportingOrigin = searchParams.get("reporting_origin") || null;
  const sort = searchParams.get("sort") === "importance" ? "importance" : "latest";
  const providerScoreGt = view === "focus" ? HIGH_SIGNAL_THRESHOLD : null;
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
  const originFacets = getReportingOriginFacets(firstPage);
  const hasActiveFilters = Boolean(q || category || level || reportingOrigin);
  const storyListRef = useRef<HTMLDivElement>(null);
  const feedIdentity = [view, q, category ?? "", level ?? "", reportingOrigin ?? "", sort].join(
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
    view?: FeedView;
  }) => {
    const params = new URLSearchParams(searchParams);
    const nextView = changes.view ?? view;
    const nextSort = changes.sort ?? sort;
    params.set("view", nextView);
    params.set("sort", nextSort);
    if (nextView === "focus") params.set("provider_score_gt", String(HIGH_SIGNAL_THRESHOLD));
    else params.delete("provider_score_gt");
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
          <p>近 12 小时高信号动态</p>
        </div>
        <NewsInlineStatus
          error={statusQuery.isError}
          fetching={statusQuery.isFetching}
          status={statusQuery.data}
        />
      </header>

      <div className="news-feed-toolbar">
        <NewsSectionTabs active={view} searchParams={searchParams} />
        <NewsFeedControls
          category={category}
          categoryFacets={categoryFacets}
          level={level}
          onChange={updateFeedParams}
          originFacets={originFacets}
          reportingOrigin={reportingOrigin}
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
              : view === "focus"
                ? "当前没有 OpenNews 评分严格高于 70 的新闻。"
                : "新闻会在下一轮采集后出现。"
          }
          title={hasActiveFilters ? "没有匹配的新闻" : "暂无新闻"}
        />
      ) : null}
      {stories.length ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <div className="news-feed-results">
            {storyFeed.count ? (
              <button className="news-new-stories" onClick={storyFeed.reveal} type="button">
                {storyFeed.count} 条新{view === "focus" ? "重点" : ""}新闻 · 回到顶部
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

function NewsSectionTabs({
  active,
  searchParams,
}: {
  active: FeedView | "brief";
  searchParams: URLSearchParams;
}) {
  return (
    <nav aria-label="新闻视图" className="news-view-tabs">
      <Link
        aria-current={active === "focus" ? "page" : undefined}
        to={{ pathname: newsPath(), search: feedTabSearch(searchParams, "focus") }}
      >
        重点
        <small>OpenNews &gt; 70</small>
      </Link>
      <Link
        aria-current={active === "all" ? "page" : undefined}
        to={{ pathname: newsPath(), search: feedTabSearch(searchParams, "all") }}
      >
        全部
      </Link>
      <Link aria-current={active === "brief" ? "page" : undefined} to={newsBriefPath()}>
        中文简报
      </Link>
    </nav>
  );
}

function NewsFeedControls({
  category,
  categoryFacets,
  level,
  onChange,
  originFacets,
  reportingOrigin,
  sort,
}: {
  category: string | null;
  categoryFacets: Facet[];
  level: NewsLevel | null;
  onChange: (changes: {
    category?: string | null;
    level?: NewsLevel | null;
    reportingOrigin?: string | null;
    sort?: "importance" | "latest";
  }) => void;
  originFacets: Facet[];
  reportingOrigin: string | null;
  sort: "importance" | "latest";
}) {
  return (
    <div className="news-filter-bar">
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
  const translation = getTranslationLayer(status);
  const readerStatus = readerFacingNewsStatus(error, status, translation);
  const lastSuccessAt = readerLastSuccessAt(status);
  const ingestReasons = status?.layers.ingest.reasons ?? [];
  const recoveringOpenNewsGap = Boolean(source?.live_connected && source.gap_unclosed);
  const hasIngestWarning = ingestReasons.some(
    (reason) => !(recoveringOpenNewsGap && reason === "opennews_gap_unclosed"),
  );
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
            <h2>新闻数据</h2>
            <dl>
              <div>
                <dt>实时连接</dt>
                <dd>{source.live_connected ? "正常" : "正在恢复"}</dd>
              </div>
              <div>
                <dt>数据缺口</dt>
                <dd>{source.gap_unclosed ? "正在补齐" : "已完整"}</dd>
              </div>
              <div>
                <dt>连续失败</dt>
                <dd>{source.consecutive_failures}</dd>
              </div>
            </dl>
          </section>
        ) : null}
        {translation ? (
          <section>
            <h2>中文标题翻译</h2>
            <dl>
              <div>
                <dt>服务状态</dt>
                <dd>{translationStateLabel(translation)}</dd>
              </div>
              <div>
                <dt>当前就绪</dt>
                <dd>
                  {translation.ready_count}/{translation.eligible_count}
                </dd>
              </div>
              <div>
                <dt>处理中</dt>
                <dd>{translation.pending_count + translation.retry_count}</dd>
              </div>
              <div>
                <dt>失败</dt>
                <dd>{translation.failed_count}</dd>
              </div>
              {translation.unavailable_count ? (
                <div>
                  <dt>不可翻译</dt>
                  <dd>{translation.unavailable_count}</dd>
                </div>
              ) : null}
              <div>
                <dt>24h 成功</dt>
                <dd>
                  {translation.rolling_24h.succeeded}/{translation.rolling_24h.attempted}
                </dd>
              </div>
              {translation.rolling_24h.success_ratio != null ? (
                <div>
                  <dt>24h 成功率</dt>
                  <dd>{formatRatio(translation.rolling_24h.success_ratio)}</dd>
                </div>
              ) : null}
              {translation.rolling_24h.latency_p95_ms != null ? (
                <div>
                  <dt>P95 耗时</dt>
                  <dd>{formatDuration(translation.rolling_24h.latency_p95_ms)}</dd>
                </div>
              ) : null}
            </dl>
            <p>只统计全球新闻列表的中文标题翻译，与飞书推送翻译相互独立。</p>
            {translation.reasons.length ? <p>部分标题翻译暂不可用，系统会继续恢复。</p> : null}
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
  const providerEvidence = story.provider_evidence;
  const metadata = providerEvidence?.provider_metadata ?? {};
  const sourceTitle = story.title;
  const translatedTitle = readyTranslatedTitle(story);
  const translationUnavailable = isTranslationUnavailable(story);
  const displayTitle = translatedTitle ?? sourceTitle;
  const summary = validSummary(story.description, displayTitle);
  const originalUrl = story.url;
  const coins = providerCoinSymbols(metadata);
  const providerScore = numericProviderScore(metadata.score);
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
    <article className="news-story-row" data-story-id={story.story_id}>
      <div className="news-story-primary">
        <header className="news-story-meta">
          {providerScore != null ? (
            <span className="news-provider-score">
              OpenNews <b>{formatPoints(providerScore)}</b>
            </span>
          ) : null}
          {providerScore != null && metadata.signal ? (
            <span className="news-provider-signal">{localizedSignal(metadata.signal)}</span>
          ) : null}
          <span className="news-severity" data-level={story.level}>
            {LEVEL_LABELS[story.level]}
          </span>
          <span>{CATEGORY_LABELS[story.category] ?? story.category}</span>
          <span>{story.source_name}</span>
          <time dateTime={new Date(story.last_published_at_ms).toISOString()}>
            {relativeTime(story.last_published_at_ms)}
          </time>
          <span>{story.source_count} 家独立来源</span>
        </header>
        <Link className="news-story-title" to={newsStoryPath(story.story_id)}>
          <h2>{displayTitle}</h2>
          {translatedTitle && translatedTitle !== sourceTitle ? (
            <p className="news-original-title">{sourceTitle}</p>
          ) : null}
          {translationUnavailable ? (
            <span className="news-translation-unavailable">暂无译文</span>
          ) : null}
        </Link>
        {summary ? <p className="news-story-summary">{summary}</p> : null}
        <div className="news-story-signals">
          <ProviderCoinBadges symbols={coins} />
          {originalUrl ? (
            <a className="news-original-link" href={originalUrl} rel="noreferrer" target="_blank">
              查看原文
              <ExternalLink aria-hidden />
            </a>
          ) : null}
        </div>
      </div>
      <div className="news-story-why">
        <b>Tracefold {story.importance_score}</b>
        <span>
          为什么重要：严重度 {formatPoints(factors.severity_points)} · 来源{" "}
          {formatPoints(factors.source_points)} · 佐证 {formatPoints(factors.corroboration_points)}{" "}
          · 时效 {formatPoints(factors.recency_points)}
          {boosts.length ? ` · ${boosts.join(" · ")}` : ""}
        </span>
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
                  <PushDeliveryBadge state={story.push_delivery_state} />
                </div>
                <p>
                  评分与 OpenNews 提供方分值相互独立。以下因子由服务端确定性计算，浏览器不重算。
                </p>
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
                    {story.provider_evidence?.url ? (
                      <a
                        className="news-audit-source-link"
                        href={story.provider_evidence.url}
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
  const sourceTitle = story.title;
  const translatedTitle = readyTranslatedTitle(story);
  const translationUnavailable = isTranslationUnavailable(story);
  const displayTitle = translatedTitle ?? sourceTitle;
  const summary = validSummary(story.description, displayTitle);
  const metadata = story.provider_evidence?.provider_metadata ?? {};
  const originalUrl = story.url;
  const coins = providerCoinSymbols(metadata);
  const providerScore = numericProviderScore(metadata.score);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const displayTitleOverflows = useTitleOverflow(titleRef, displayTitle);
  const displayTitleIsLong =
    displayTitle.length > DETAIL_TITLE_EXPANSION_THRESHOLD || displayTitleOverflows;
  const sourceTitleIsLong =
    translatedTitle != null && sourceTitle.length > DETAIL_TITLE_EXPANSION_THRESHOLD;
  return (
    <header className="news-story-hero">
      <div className="news-story-badges">
        <span data-level={story.level}>{LEVEL_LABELS[story.level]}</span>
        <span>{CATEGORY_LABELS[story.category] ?? story.category}</span>
        <span>{story.source_name}</span>
        {providerScore != null ? (
          <span>
            OpenNews {formatPoints(providerScore)}
            {metadata.signal ? ` · ${localizedSignal(metadata.signal)}` : ""}
          </span>
        ) : null}
        <ProviderCoinBadges symbols={coins} />
      </div>
      <h1 className="is-clamped" ref={titleRef}>
        {displayTitle}
      </h1>
      {translatedTitle && translatedTitle !== sourceTitle ? (
        <p className="news-original-title" title={sourceTitle}>
          {sourceTitle}
        </p>
      ) : null}
      {displayTitleIsLong || sourceTitleIsLong ? (
        <details className="news-title-expansion">
          <summary>
            <span className="news-title-expand-label">展开完整标题</span>
            <span className="news-title-collapse-label">收起完整标题</span>
          </summary>
          {displayTitleIsLong ? <p>{displayTitle}</p> : null}
          {sourceTitleIsLong ? (
            <div>
              <b>原文</b>
              <p>{sourceTitle}</p>
            </div>
          ) : null}
        </details>
      ) : null}
      {translationUnavailable ? (
        <span className="news-translation-unavailable">暂无译文</span>
      ) : null}
      {summary ? <p className="news-story-lead">{summary}</p> : null}
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

function ProviderCoinBadges({ symbols }: { symbols: string[] }) {
  if (!symbols.length) return null;
  return (
    <span aria-label="OpenNews 关联代币" className="news-story-coins" role="group">
      {symbols.slice(0, 3).map((symbol) => (
        <b key={symbol}>{symbol}</b>
      ))}
      {symbols.length > 3 ? <small>+{symbols.length - 3}</small> : null}
    </span>
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
        <ProviderEvidenceDisclosure metadata={member.provider_metadata} />
      </div>
    </article>
  );
}

function ProviderEvidenceDisclosure({ metadata }: { metadata: NewsProviderMetadata }) {
  if (!hasProviderMetadata(metadata)) return null;
  return (
    <details className="news-provider-disclosure">
      <summary>OpenNews 元数据</summary>
      <div>
        <ProviderFacts metadata={metadata} />
        <ProviderCoins metadata={metadata} />
      </div>
    </details>
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

function PushDeliveryBadge({ state }: { state: NewsPushDeliveryState | null }) {
  if (state == null) return null;
  return (
    <span className="news-push-state" data-state={state}>
      {PUSH_DELIVERY_LABELS[state]}
    </span>
  );
}

function ProviderFacts({ metadata }: { metadata: NewsProviderMetadata }) {
  const providerScore = numericProviderScore(metadata.score);
  if (providerScore == null && !metadata.signal && !metadata.grade && !metadata.source) {
    return null;
  }
  return (
    <dl className="news-provider-facts">
      {providerScore != null ? (
        <div>
          <dt>OpenNews 评分</dt>
          <dd>{formatPoints(providerScore)}</dd>
        </div>
      ) : null}
      {metadata.signal ? (
        <div>
          <dt>信号</dt>
          <dd>{localizedSignal(metadata.signal)}</dd>
        </div>
      ) : null}
      {metadata.grade ? (
        <div>
          <dt>等级</dt>
          <dd>{metadata.grade}</dd>
        </div>
      ) : null}
      {metadata.source ? (
        <div>
          <dt>提供方来源</dt>
          <dd>{metadata.source}</dd>
        </div>
      ) : null}
    </dl>
  );
}

function ProviderCoins({ metadata }: { metadata: NewsProviderMetadata }) {
  const coins = metadata.coins ?? [];
  if (!coins.length) return null;
  return (
    <div className="news-provider-coins">
      <b>关联代币</b>
      <ul aria-label="OpenNews 关联代币">
        {coins.map((coin, index) => (
          <li key={`${coin.symbol}:${coin.market_type}:${index}`}>{formatProviderCoin(coin)}</li>
        ))}
      </ul>
    </div>
  );
}

function WorldBriefRoute({ token }: { token: string }) {
  const [searchParams] = useSearchParams();
  const query = useNewsBriefWithToken(token);
  const brief = query.data;
  return (
    <section
      aria-label="中文新闻简报"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <NewsSectionTabs active="brief" searchParams={searchParams} />
      <header className="news-toolbar news-brief-toolbar">
        <div>
          <span className="news-eyebrow">中文研判</span>
          <h1>全球新闻简报</h1>
        </div>
        <span className="news-brief-state" data-state={brief?.state ?? "unavailable"}>
          {briefStateLabel(brief?.state)}
        </span>
      </header>
      {query.isLoading && !brief ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取中文简报" />
      ) : null}
      {query.isError && !brief ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {brief && !brief.publication ? (
        <PageState.Empty
          hint={
            brief.latest_run?.last_error ||
            `当前 ${brief.candidate_story_count} 个新闻事件、${brief.candidate_source_count} 个来源；至少需要 3 个新闻事件和 2 个来源。`
          }
          title={
            brief.state === "failed"
              ? "简报生成失败"
              : brief.state === "insufficient_material"
                ? "材料还不足"
                : "尚无中文简报"
          }
        />
      ) : null}
      {brief?.publication ? (
        <BriefDocument publication={brief.publication} state={brief.state} />
      ) : null}
      {brief?.history.length ? (
        <details className="news-brief-history">
          <summary>历史发布 · {brief.history.length}</summary>
          <div className="news-brief-history-list">
            {brief.history.map((publication) => (
              <div key={publication.publication_id}>
                <time>{absoluteTime(publication.published_at_ms)}</time>
                <span>{publication.model}</span>
                <span>已验证</span>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

function BriefDocument({ publication, state }: { publication: BriefPublication; state: string }) {
  return (
    <article className="news-brief-document">
      <header>
        <span>{state === "stale_fallback" ? "材料已变化，展示上一版" : "当前发布"}</span>
        <p>
          证据截止 {absoluteTime(publication.evidence_cutoff_at_ms)} · 发布{" "}
          {absoluteTime(publication.published_at_ms)}
        </p>
      </header>
      <section className="news-brief-lead">
        <h2>总览</h2>
        <p>{publication.lead}</p>
      </section>
      <ol className="news-brief-lines">
        {publication.lines.map((line, index) => {
          const source = publication.sources[index];
          const sourceUrl = validExternalUrl(source?.url);
          return (
            <li key={source?.story_id ?? index}>
              <p>{line}</p>
              {source ? (
                <div>
                  <Link to={newsStoryPath(source.story_id)}>查看证据</Link>
                  {sourceUrl ? (
                    <a href={sourceUrl} rel="noreferrer" target="_blank">
                      {source.source}
                      <ExternalLink aria-hidden />
                    </a>
                  ) : (
                    <span>{source.source}</span>
                  )}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      <footer>
        {publication.provider}/{publication.model} · 引用锁定{" "}
        {publication.validation.citation_index_lock ? "通过" : "失败"}
      </footer>
    </article>
  );
}

function parseLevel(value: string | null): NewsLevel | null {
  return value && value in LEVEL_LABELS ? (value as NewsLevel) : null;
}

function setOptionalParam(params: URLSearchParams, name: string, value: string | null | undefined) {
  if (value) params.set(name, value);
  else params.delete(name);
}

function validExternalUrl(value: string | null | undefined): string | null {
  const normalized = value?.trim() ?? "";
  return /^https?:\/\//i.test(normalized) ? normalized : null;
}

function feedTabSearch(searchParams: URLSearchParams, view: FeedView): string {
  const params = new URLSearchParams(searchParams);
  params.set("view", view);
  if (!params.has("sort")) params.set("sort", "latest");
  if (view === "focus") params.set("provider_score_gt", String(HIGH_SIGNAL_THRESHOLD));
  else params.delete("provider_score_gt");
  const value = params.toString();
  return value ? `?${value}` : "";
}

function getReportingOriginFacets(page?: NewsFeed): Facet[] {
  if (!page) return [];
  const facets = page.facets as NewsFeed["facets"] & { reporting_origins?: unknown };
  const reportingOrigins = normalizeFacets(facets.reporting_origins);
  if (reportingOrigins.length) return reportingOrigins;
  return page.facets.sources.map((facet) => ({
    count: facet.count,
    label: facet.label,
    value: facet.value,
  }));
}

function normalizeFacets(value: unknown): Facet[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    if (!isRecord(entry) || typeof entry.value !== "string" || typeof entry.count !== "number") {
      return [];
    }
    return [
      {
        count: entry.count,
        label: typeof entry.label === "string" ? entry.label : undefined,
        value: entry.value,
      },
    ];
  });
}

function withSelectedFacet(facets: Facet[], selected: string | null): Facet[] {
  if (!selected || facets.some((facet) => facet.value === selected)) return facets;
  return [{ count: 0, label: selected, value: selected }, ...facets];
}

function readyTranslatedTitle(story: NewsStory): string | null {
  const translation = exactTitleTranslation(story);
  if (!translation || translation.state !== "ready" || !translation.title_zh) {
    return null;
  }
  return translation.title_zh;
}

function isTranslationUnavailable(story: NewsStory): boolean {
  const translation = exactTitleTranslation(story);
  return translation?.state === "failed" || translation?.state === "unavailable";
}

function exactTitleTranslation(story: NewsStory): NewsTitleTranslation | null {
  const translation = story.title_translation;
  return translation?.source_title === story.title ? translation : null;
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

function providerCoinSymbols(metadata: NewsProviderMetadata): string[] {
  const seen = new Set<string>();
  return (metadata.coins ?? []).flatMap((coin) => {
    const symbol = coin.symbol.trim();
    const identity = symbol.toLowerCase();
    if (!symbol || seen.has(identity)) return [];
    seen.add(identity);
    return [symbol];
  });
}

function numericProviderScore(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function hasProviderMetadata(metadata: NewsProviderMetadata): boolean {
  return Boolean(
    numericProviderScore(metadata.score) != null ||
    metadata.signal ||
    metadata.grade ||
    metadata.source ||
    metadata.coins?.length,
  );
}

function getTranslationLayer(status?: NewsStatus): NewsTranslationLayer | null {
  return status?.layers.translation ?? null;
}

function localizedSignal(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (["long", "bullish", "positive", "buy"].includes(normalized)) return "偏多";
  if (["short", "bearish", "negative", "sell"].includes(normalized)) return "偏空";
  if (["neutral", "hold", "mixed"].includes(normalized)) return "中性";
  return value;
}

function translationStateLabel(translation: NewsTranslationLayer): string {
  if (!translation.configured) return "未启用";
  if (translation.status === "ready") return "正常";
  if (translation.status === "warming") return "启动中";
  return "部分异常";
}

function readerFacingNewsStatus(
  error: boolean,
  status: NewsStatus | undefined,
  translation: NewsTranslationLayer | null,
): { label: string; state: "live" | "recovering" | "stalled" } {
  if (error) return { label: "状态暂不可用", state: "stalled" };
  if (!status) return { label: "正在检查新闻", state: "recovering" };
  if (status.layers.story.status === "degraded") {
    return { label: "新闻更新异常", state: "stalled" };
  }
  if (status.layers.ingest.status === "degraded") {
    const opennews = status.layers.ingest.opennews;
    if (opennews?.live_connected && opennews.gap_unclosed) {
      return { label: "历史新闻补齐中", state: "recovering" };
    }
    return { label: "新闻更新异常", state: "stalled" };
  }
  const factStates = [status.layers.ingest.status, status.layers.story.status];
  if (factStates.includes("warming")) {
    return { label: "新闻数据恢复中", state: "recovering" };
  }
  if (translation?.status === "warming") {
    return { label: "中文标题补齐中", state: "live" };
  }
  if (translation?.status === "degraded") {
    return { label: "部分中文标题暂不可用", state: "recovering" };
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

function briefStateLabel(state?: string): string {
  if (state === "ready") return "已就绪";
  if (state === "running") return "正在生成";
  if (state === "stale_fallback") return "展示上一版";
  if (state === "insufficient_material") return "材料不足";
  if (state === "failed") return "生成失败";
  return "不可用";
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

function formatProviderCoin(coin: NewsProviderCoin): string {
  const identity = [coin.symbol, coin.market_type, coin.match].filter(Boolean).join(" · ");
  const annotations = [
    coin.score != null ? `评分 ${coin.score}` : "",
    coin.signal ? localizedSignal(coin.signal) : "",
    coin.grade ? `等级 ${coin.grade}` : "",
  ].filter(Boolean);
  return annotations.length ? `${identity} · ${annotations.join(" · ")}` : identity;
}

function formatRatio(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function formatDuration(value: number): string {
  return value < 1_000 ? `${Math.round(value)} ms` : `${(value / 1_000).toFixed(1)} 秒`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
