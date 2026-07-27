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
  const groups = query.data?.categories ?? [];
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
          <p>先在 96 小时全局窗口聚类，再按分类各取前 20；浏览器不聚类、不评分、不重排。</p>
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
        {Object.entries(CATEGORY_LABELS).map(([value, label]) => (
          <button
            aria-pressed={category === value}
            key={value}
            onClick={() => setFeedParams({ category: value })}
            type="button"
          >
            {label}
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
      {!query.isLoading && !query.isError && !groups.length ? (
        <PageState.Empty title="暂无活跃 Story" hint="新闻会在下一轮两分钟采集后出现。" />
      ) : null}
      {groups.length ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <div className="news-category-groups">
            {groups.map((group) => (
              <section className="news-category-group" key={group.category}>
                <header>
                  <h3>{CATEGORY_LABELS[group.category] ?? group.category}</h3>
                  <span>{group.stories.length} 个 Story</span>
                </header>
                <div className="news-story-list">
                  {group.stories.map((story) => (
                    <StoryCard key={story.story_id} story={story} />
                  ))}
                </div>
              </section>
            ))}
          </div>
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
        <PageState.Empty title="尚无来源状态" hint="news_pipeline 首轮同步后会显示来源。" />
      ) : null}
      {sources.length ? (
        <div className="news-source-list">
          {sources.map((source) => (
            <article className="news-source-card" key={source.source_id}>
              <header>
                <div>
                  <h2>{source.name}</h2>
                  <span>
                    Tier {source.tier} · {source.lang} · {source.reporting_origin}
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
          <time>{relativeTime(story.last_published_at_ms)}</time>
        </span>
        <strong>{story.title}</strong>
        <small>{story.description || "原始来源未提供有效摘要"}</small>
        <span className="news-factor-line">
          严重度 {formatPoints(factors.severity_points)} · 来源{" "}
          {formatPoints(factors.source_points)}
          {" · "}独立源 {formatPoints(factors.corroboration_points)} · 时效{" "}
          {formatPoints(factors.recency_points)}
          {factors.diplomacy_flashpoint_boost
            ? ` · 地缘信号 +${factors.diplomacy_flashpoint_boost}`
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
            <span data-level={story.level}>{LEVEL_LABELS[story.level]}</span>
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
            <h2>重要度因子</h2>
            <dl className="news-factor-grid">
              <div>
                <dt>严重度</dt>
                <dd>{formatPoints(story.importance_factors.severity_points)}</dd>
              </div>
              <div>
                <dt>来源层级</dt>
                <dd>{formatPoints(story.importance_factors.source_points)}</dd>
              </div>
              <div>
                <dt>独立报道源</dt>
                <dd>{formatPoints(story.importance_factors.corroboration_points)}</dd>
              </div>
              <div>
                <dt>24 小时时效</dt>
                <dd>{formatPoints(story.importance_factors.recency_points)}</dd>
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
                    <span>{member.source_name}</span>
                    <span>Tier {member.tier}</span>
                    <span>{member.reporting_origin}</span>
                    <time>{absoluteTime(member.published_at_ms)}</time>
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
          title={brief.state === "failed" ? "Brief 生成失败" : "尚无 World Brief"}
          hint={brief.last_error || "有候选 Story 后，后台每十分钟生成一次。"}
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
              <span>{publication.status === "degraded" ? "含确定性回退" : "通过验证"}</span>
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
        <span>{state === "updating" ? "正在更新，当前展示上一版" : "当前发布"}</span>
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
  if (state === "fresh") return "新鲜";
  if (state === "updating") return "更新中（保留上一版）";
  if (state === "stale") return "已陈旧";
  if (state === "failed") return "生成失败";
  return "不可用";
}

function sourceStatusLabel(state?: string | null): string {
  if (state === "success") return "成功";
  if (state === "not_modified") return "无变化";
  if (state === "failed") return "失败";
  return "待采集";
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
