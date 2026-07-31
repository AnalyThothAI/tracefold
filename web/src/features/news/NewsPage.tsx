import { newsBriefPath, newsPath, newsSourcesPath, newsStoryPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { ArrowLeft, ExternalLink, Newspaper, Radio } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import "./news.css";
import "./newsDetail.css";
import {
  type BriefPublication,
  type NewsStory,
  useNewsBriefWithToken,
  useNewsFeedWithToken,
  useNewsSourcesWithToken,
  useNewsStoryWithToken,
} from "./useNewsPage";

type NewsPageProps = {
  brief?: boolean;
  sources?: boolean;
  storyId?: string | null;
  token: string;
};

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

const LEVEL_LABELS = {
  critical: "严重",
  high: "高",
  medium: "中",
  low: "低",
  info: "信息",
} as const;

export function NewsPage({ brief = false, sources = false, token, storyId = null }: NewsPageProps) {
  if (brief) return <WorldBriefRoute token={token} />;
  if (sources) return <SourcesRoute token={token} />;
  if (storyId) return <StoryRoute storyId={storyId} token={token} />;
  return <FeedRoute token={token} />;
}

function FeedRoute({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const category = searchParams.get("category") || null;
  const sort = searchParams.get("sort") === "latest" ? "latest" : "importance";
  const query = useNewsFeedWithToken(token, category, sort);
  const pages = query.data?.pages ?? [];
  const stories = Array.from(
    new Map(pages.flatMap((page) => page.stories).map((story) => [story.story_id, story])).values(),
  );
  const categoryFacets = pages[0]?.facets.categories ?? [];
  const setFeedParams = (next: { category?: string | null; sort?: "importance" | "latest" }) => {
    const params = new URLSearchParams(searchParams);
    const nextCategory = next.category === undefined ? category : next.category;
    const nextSort = next.sort ?? sort;
    if (nextCategory) params.set("category", nextCategory);
    else params.delete("category");
    if (nextSort === "latest") params.set("sort", "latest");
    else params.delete("sort");
    setSearchParams(params, { replace: true });
  };

  return (
    <section
      aria-label="全球新闻 Story 流"
      className="radar-panel news-panel news-story-shell"
      data-page-archetype="scan"
    >
      <header className="news-story-header">
        <div>
          <span className="news-eyebrow">
            <Radio aria-hidden />
            WorldMonitor Story pipeline
          </span>
          <h2>全球新闻 Story 流</h2>
          <p>
            96 小时窗口全局聚类后的单一 Story 流；分类只是过滤器，浏览器不聚类、不评分、不重排。
          </p>
        </div>
        <div className="news-header-actions">
          <Link className="news-back-link" to={newsSourcesPath()}>
            来源状态
          </Link>
          <Link className="news-back-link" to={newsBriefPath()}>
            <Newspaper aria-hidden />
            中文 World Brief
          </Link>
        </div>
      </header>

      <nav aria-label="新闻分类" className="news-category-nav">
        <button
          aria-pressed={!category}
          onClick={() => setFeedParams({ category: null })}
          type="button"
        >
          全部
        </button>
        {categoryFacets.map((facet) => (
          <button
            aria-pressed={category === facet.value}
            key={facet.value}
            onClick={() => setFeedParams({ category: facet.value })}
            type="button"
          >
            {CATEGORY_LABELS[facet.value] ?? facet.value} {facet.count}
          </button>
        ))}
      </nav>
      <nav aria-label="新闻排序" className="news-sort-nav">
        <span>排序</span>
        <button
          aria-pressed={sort === "importance"}
          onClick={() => setFeedParams({ sort: "importance" })}
          type="button"
        >
          重要度
        </button>
        <button
          aria-pressed={sort === "latest"}
          onClick={() => setFeedParams({ sort: "latest" })}
          type="button"
        >
          最新
        </button>
      </nav>

      {query.isLoading && !query.data ? (
        <PageState.Loading layout="panel" rows={8} label="正在读取 Story 流" />
      ) : null}
      {query.isError && !query.data ? <PageState.Error error={query.error} /> : null}
      {!query.isLoading && !query.isError && !stories.length ? (
        <PageState.Empty title="暂无活跃 Story" hint="新闻会在下一轮两分钟采集后出现。" />
      ) : null}
      {stories.length ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <div className="news-story-list">
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
              {query.isFetchingNextPage ? "正在加载" : "加载更多"}
            </button>
          ) : null}
        </PageState.Stale>
      ) : null}
    </section>
  );
}

function SourcesRoute({ token }: { token: string }) {
  const query = useNewsSourcesWithToken(token);
  const sources = query.data?.items ?? [];
  return (
    <section
      aria-label="新闻来源状态"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <header className="news-toolbar">
        <Link className="news-back-link" to={newsPath()}>
          <ArrowLeft aria-hidden />
          返回 Story 流
        </Link>
        <span className="news-live-state">{query.isFetching ? "刷新中" : "每 60 秒检查"}</span>
      </header>
      <header className="news-source-heading">
        <div>
          <span className="news-eyebrow">RSS / Atom</span>
          <h1>新闻来源状态</h1>
          <p>每个来源独立轮询、独立退避；空 Feed 与采集失败分别记录。</p>
        </div>
        <b>{sources.length} 个来源</b>
      </header>
      {query.isLoading && !query.data ? (
        <PageState.Loading layout="panel" rows={8} label="正在读取来源状态" />
      ) : null}
      {query.isError && !query.data ? <PageState.Error error={query.error} /> : null}
      {!query.isLoading && !query.isError && !sources.length ? (
        <PageState.Empty title="尚无来源状态" hint="News 首轮来源同步后会显示来源。" />
      ) : null}
      {sources.length ? (
        <div className="news-source-list">
          {sources.map((source) => (
            <article className="news-source-card" key={source.source_id}>
              <header>
                <div>
                  <h2>{source.name}</h2>
                  <span>
                    Tier {source.tier} · {source.lang} · {source.memberships.join(" / ")}
                  </span>
                </div>
                <b data-status={source.latest_fetch_status ?? "pending"}>
                  {sourceStatusLabel(source.latest_fetch_status)}
                </b>
              </header>
              <dl>
                <div>
                  <dt>上次成功</dt>
                  <dd>
                    {source.last_success_at_ms
                      ? relativeTime(source.last_success_at_ms)
                      : "尚未成功"}
                  </dd>
                </div>
                <div>
                  <dt>耗时</dt>
                  <dd>
                    {source.latest_fetch_duration_ms == null
                      ? "—"
                      : `${source.latest_fetch_duration_ms} ms`}
                  </dd>
                </div>
                <div>
                  <dt>解析 / 入库</dt>
                  <dd>
                    {source.latest_entries_seen ?? 0} /{" "}
                    {(source.latest_items_inserted ?? 0) + (source.latest_items_updated ?? 0)}
                  </dd>
                </div>
                <div>
                  <dt>获取路径</dt>
                  <dd>{sourcePathLabel(source.latest_fetch_path)}</dd>
                </div>
                <div>
                  <dt>连续失败</dt>
                  <dd>{source.consecutive_failures}</dd>
                </div>
              </dl>
              {source.latest_rejection_counts &&
              Object.keys(source.latest_rejection_counts).length ? (
                <p>
                  门禁：
                  {Object.entries(source.latest_rejection_counts)
                    .map(([reason, count]) => `${reason} ${count}`)
                    .join(" · ")}
                </p>
              ) : null}
              {source.last_error ? <p className="news-source-error">{source.last_error}</p> : null}
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function StoryCard({ story }: { story: NewsStory }) {
  const factors = story.importance_factors;
  return (
    <Link className="news-story-row" to={newsStoryPath(story.story_id)}>
      <span className="news-importance" data-level={story.level}>
        <b>{story.importance_score}</b>
        <small>重要度</small>
      </span>
      <span className="news-story-copy">
        <span className="news-story-meta">
          <b data-level={story.level}>{LEVEL_LABELS[story.level]}</b>
          <span>{story.source_name}</span>
          <time dateTime={new Date(story.last_published_at_ms).toISOString()}>
            {relativeTime(story.last_published_at_ms)}
          </time>
        </span>
        <strong>{story.title}</strong>
        <small>{story.description || "原始来源未提供有效摘要"}</small>
        <span className="news-factor-line">
          严重度得分 {formatPoints(factors.severity_points)} · 来源质量得分{" "}
          {formatPoints(factors.source_points)}（Tier {factors.source_tier}） · 佐证得分{" "}
          {formatPoints(factors.corroboration_points)}（计分来源{" "}
          {factors.scoring_corroboration_count}） · 时效得分 {formatPoints(factors.recency_points)}
          {factors.diplomacy_flashpoint_boost
            ? ` · 外交热点 +${factors.diplomacy_flashpoint_boost}`
            : ""}
          {factors.entity_corroboration_boost
            ? ` · 实体佐证 +${factors.entity_corroboration_boost}`
            : ""}
        </span>
      </span>
      <span className="news-story-counts">
        <b>{story.item_count}</b>
        <small>NewsItem</small>
        <b>{story.source_count}</b>
        <small>独立报道源</small>
      </span>
    </Link>
  );
}

function StoryRoute({ token, storyId }: { token: string; storyId: string }) {
  const query = useNewsStoryWithToken(token, storyId);
  const story = query.data;
  const representativeMember = story?.members.find(
    (member) => member.item_id === story.representative_item_id,
  );
  const scoringMember = story?.members.find((member) => member.item_id === story.scoring_item_id);
  return (
    <section
      aria-label="Story 事实页"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <header className="news-toolbar">
        <Link className="news-back-link" to={newsPath()}>
          <ArrowLeft aria-hidden />
          返回 Story 流
        </Link>
        <span className="news-live-state">{query.isFetching ? "刷新中" : "事实已持久化"}</span>
      </header>
      {query.isLoading && !story ? (
        <PageState.Loading layout="panel" rows={6} label="正在读取 Story" />
      ) : null}
      {query.isError && !story ? <PageState.Error error={query.error} /> : null}
      {story ? (
        <article className="news-story-detail">
          <header className="news-story-hero">
            <div className="news-story-badges">
              <span data-level={story.level}>{LEVEL_LABELS[story.level]}</span>
              <span data-active={story.active}>{story.active ? "活跃 Story" : "已归档 Story"}</span>
            </div>
            <h1>{story.title}</h1>
            <p>{story.description || "原始来源未提供有效摘要"}</p>
            <div className="news-evidence-metrics">
              <span>
                <b>{story.importance_score}</b>重要度
              </span>
              <span>
                <b>{story.item_count}</b>NewsItem
              </span>
              <span>
                <b>{story.source_count}</b>独立报道源
              </span>
              <span>
                <b>{relativeTime(story.last_published_at_ms)}</b>最后报道
              </span>
            </div>
          </header>

          <section className="news-detail-card">
            <h2>Story 聚合身份</h2>
            <dl className="news-story-identity-grid">
              <div>
                <dt>展示代表</dt>
                <dd>{representativeMember?.source_name ?? story.source_name}</dd>
                <code>{story.representative_item_id}</code>
              </div>
              <div>
                <dt>评分依据</dt>
                <dd>{scoringMember?.source_name ?? "评分 NewsItem"}</dd>
                <code>{story.scoring_item_id}</code>
              </div>
            </dl>
          </section>

          <section className="news-detail-card">
            <h2>重要度因子</h2>
            <p>
              来源数量与得分分开显示；计分佐证来源取 Story 内物理来源与 24
              小时实体信号来源的较大值，最多按 5 个来源计入佐证得分。
            </p>
            <dl className="news-factor-grid">
              <div>
                <dt>严重度得分</dt>
                <dd>{formatPoints(story.importance_factors.severity_points)}</dd>
                <small>{LEVEL_LABELS[story.importance_factors.severity_level]}严重度</small>
              </div>
              <div>
                <dt>来源质量得分</dt>
                <dd>{formatPoints(story.importance_factors.source_points)}</dd>
                <small>
                  {scoringMember?.source_name ?? "评分 NewsItem"} · Tier{" "}
                  {story.importance_factors.source_tier}
                </small>
              </div>
              <div>
                <dt>Story 内物理来源</dt>
                <dd>{story.importance_factors.physical_source_count}</dd>
                <small>聚类成员中的不同物理来源</small>
              </div>
              <div>
                <dt>计分佐证来源</dt>
                <dd>{story.importance_factors.scoring_corroboration_count}</dd>
                <small>Story 内来源与 24 小时实体信号来源取较大值</small>
              </div>
              <div>
                <dt>佐证得分</dt>
                <dd>{formatPoints(story.importance_factors.corroboration_points)}</dd>
                <small>最多按 5 个来源计分</small>
              </div>
              <div>
                <dt>24 小时时效得分</dt>
                <dd>{formatPoints(story.importance_factors.recency_points)}</dd>
              </div>
              <div>
                <dt>外交热点加分</dt>
                <dd>+{formatPoints(story.importance_factors.diplomacy_flashpoint_boost)}</dd>
              </div>
              <div>
                <dt>实体佐证加分</dt>
                <dd>+{formatPoints(story.importance_factors.entity_corroboration_boost)}</dd>
              </div>
              <div>
                <dt>总重要度</dt>
                <dd>{story.importance_factors.total}</dd>
                <small>基础四项四舍五入后加额外加分</small>
              </div>
            </dl>
          </section>

          <section className="news-detail-card">
            <h2>聚类成员</h2>
            <p>每个 NewsItem 是来源当前事实；同一条目的 pubDate 漂移不会生成修订。</p>
            <div className="news-member-list">
              {story.members.map((member) => (
                <article className="news-member-card" key={member.item_id}>
                  <header>
                    <span>{member.current ? "当前成员" : "历史成员"}</span>
                    <span>{member.source_name}</span>
                    <span>Tier {member.tier}</span>
                    <span>{member.reporting_origin}</span>
                    <time>{absoluteTime(member.published_at_ms)}</time>
                    <span>加入 {absoluteTime(member.first_joined_at_ms)}</span>
                    <span>确认 {absoluteTime(member.last_confirmed_at_ms)}</span>
                    {member.item_id === story.representative_item_id ? <span>展示代表</span> : null}
                    {member.item_id === story.scoring_item_id ? <span>评分依据</span> : null}
                  </header>
                  <h3>{member.title}</h3>
                  {member.description ? <p>{member.description}</p> : null}
                  <a href={member.url} rel="noreferrer" target="_blank">
                    阅读原文
                    <ExternalLink aria-hidden />
                  </a>
                </article>
              ))}
            </div>
          </section>
        </article>
      ) : null}
    </section>
  );
}

function WorldBriefRoute({ token }: { token: string }) {
  const query = useNewsBriefWithToken(token);
  const brief = query.data;
  return (
    <section
      aria-label="中文 World Brief"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <header className="news-toolbar">
        <Link className="news-back-link" to={newsPath()}>
          <ArrowLeft aria-hidden />
          返回 Story 流
        </Link>
        <span className="news-brief-state" data-state={brief?.state ?? "unavailable"}>
          {briefStateLabel(brief?.state)}
        </span>
      </header>
      {query.isLoading && !brief ? (
        <PageState.Loading layout="panel" rows={6} label="正在读取 World Brief" />
      ) : null}
      {query.isError && !brief ? <PageState.Error error={query.error} /> : null}
      {brief && !brief.publication ? (
        <PageState.Empty
          title={
            brief.state === "failed"
              ? "Brief 生成失败"
              : brief.state === "insufficient_material"
                ? "确定性选材不足"
                : "尚无 World Brief"
          }
          hint={
            brief.latest_run?.last_error ||
            `当前 ${brief.candidate_story_count} 个 Story、${brief.candidate_source_count} 个物理来源；至少需要 3 个 Story 和 2 个来源。`
          }
        />
      ) : null}
      {brief?.publication ? (
        <BriefDocument publication={brief.publication} state={brief.state} />
      ) : null}
      {brief?.history.length ? (
        <section className="news-brief-history">
          <h2>历史发布</h2>
          {brief.history.map((publication) => (
            <div key={publication.publication_id}>
              <time>{absoluteTime(publication.published_at_ms)}</time>
              <span>{publication.model}</span>
              <span>通过验证</span>
            </div>
          ))}
        </section>
      ) : null}
    </section>
  );
}

function BriefDocument({ publication, state }: { publication: BriefPublication; state: string }) {
  return (
    <article className="news-brief-document">
      <header>
        <span>{state === "stale_fallback" ? "当前材料已变化，展示上一版" : "当前发布"}</span>
        <h1>全球新闻简报</h1>
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
          return (
            <li key={source?.story_id ?? index}>
              <p>{line}</p>
              {source ? (
                <div>
                  <Link to={newsStoryPath(source.story_id)}>查看 Story</Link>
                  <a href={source.url} rel="noreferrer" target="_blank">
                    {source.source}
                    <ExternalLink aria-hidden />
                  </a>
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      <footer>
        {publication.provider}/{publication.model} · 引用序号锁定{" "}
        {publication.validation.citation_index_lock ? "通过" : "失败"}
      </footer>
    </article>
  );
}

function briefStateLabel(state?: string): string {
  if (state === "ready") return "已就绪";
  if (state === "running") return "AI 正在处理";
  if (state === "stale_fallback") return "上一版（材料已变化）";
  if (state === "insufficient_material") return "选材不足";
  if (state === "failed") return "生成失败";
  return "不可用";
}

function sourceStatusLabel(state?: string | null): string {
  if (state === "success") return "成功";
  if (state === "not_modified") return "无变化";
  if (state === "failed") return "失败";
  return "待采集";
}

function sourcePathLabel(path?: "direct" | "relay" | null): string {
  if (path === "relay") return "Relay 回退";
  if (path === "direct") return "直连";
  return "尚未尝试";
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
