import { newsPath, newsStoryPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Radio,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import "./news.css";
import "./newsDetail.css";
import {
  analysisLabel,
  phaseLabel,
  type NewsArticle,
  type NewsStoryDetail,
  type NewsStorySummary,
  type NewsVerificationStatus,
  verificationLabel,
} from "./model/newsStoryViewModel";
import { NEWS_PAGE_SIZE, useNewsStoriesWithToken, useNewsStoryWithToken } from "./useNewsPage";

type NewsPageProps = {
  token: string;
  storyId?: string | null;
};

type CursorState = {
  queryKey: string;
  stack: Array<string | null>;
};

const EMPTY_STORIES: NewsStorySummary[] = [];
const VERIFICATION_FILTERS = [
  "all",
  "corroborated",
  "trusted",
  "attributed",
  "unverified",
] as const;

export function NewsPage({ token, storyId = null }: NewsPageProps) {
  return storyId ? (
    <NewsStoryRoute storyId={storyId} token={token} />
  ) : (
    <NewsStoryListRoute token={token} />
  );
}

function NewsStoryListRoute({ token }: { token: string }) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const searchQuery = searchParams.get("q") ?? "";
  const sourceQuery = searchParams.get("source") ?? "";
  const verification = normalizeVerification(searchParams.get("verification"));
  const queryKey = `${searchQuery}\n${sourceQuery}\n${verification}`;
  const [cursorState, setCursorState] = useState<CursorState>(() => ({
    queryKey,
    stack: [null],
  }));
  const cursorStack = cursorState.queryKey === queryKey ? cursorState.stack : [null];
  const cursor = cursorStack[cursorStack.length - 1] ?? null;
  const query = useNewsStoriesWithToken(token, {
    cursor,
    q: searchQuery.trim() || null,
    source: sourceQuery.trim() || null,
    verificationStatus: verification === "all" ? null : verification,
  });
  const stories = query.data?.items ?? EMPTY_STORIES;

  useEffect(() => {
    setCursorState((state) => (state.queryKey === queryKey ? state : { queryKey, stack: [null] }));
  }, [queryKey]);

  const updateParam = (name: string, value: string) => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value.trim() && value !== "all") next.set(name, value);
        else next.delete(name);
        return next;
      },
      { replace: true },
    );
  };

  return (
    <section
      aria-label="Global news stories"
      className="radar-panel news-panel news-story-shell"
      data-page-archetype="scan"
    >
      <header className="news-story-header">
        <div>
          <span className="news-eyebrow">
            <Radio aria-hidden />
            Global intelligence
          </span>
          <h2>全球政治经济新闻流</h2>
          <p>一个事件只显示为一个 Story；来源、核验状态与 AI 判断分开呈现。</p>
        </div>
        <StoryPager
          hasNextPage={Boolean(query.data?.next_cursor)}
          isFetching={query.isFetching}
          pageNumber={cursorStack.length}
          rowCount={stories.length}
          onNext={() => {
            const nextCursor = query.data?.next_cursor;
            if (!nextCursor) return;
            setCursorState((state) => ({
              queryKey,
              stack: [...(state.queryKey === queryKey ? state.stack : [null]), nextCursor],
            }));
          }}
          onPrevious={() =>
            setCursorState((state) => {
              const stack = state.queryKey === queryKey ? state.stack : [null];
              return {
                queryKey,
                stack: stack.length > 1 ? stack.slice(0, -1) : stack,
              };
            })
          }
        />
      </header>

      <div className="news-story-filters" aria-label="News Story filters">
        <label>
          <span>搜索事件</span>
          <input
            aria-label="Search stories"
            onChange={(event) => updateParam("q", event.target.value)}
            placeholder="标题或摘要"
            value={searchQuery}
          />
        </label>
        <label>
          <span>来源</span>
          <input
            aria-label="Filter by source"
            onChange={(event) => updateParam("source", event.target.value)}
            placeholder="Reuters、6551…"
            value={sourceQuery}
          />
        </label>
        <div className="news-verification-filters" aria-label="Verification filters">
          {VERIFICATION_FILTERS.map((value) => (
            <button
              aria-pressed={verification === value}
              key={value}
              onClick={() => updateParam("verification", value)}
              type="button"
            >
              {value === "all" ? "全部" : verificationLabel(value)}
            </button>
          ))}
        </div>
      </div>

      <div className="news-story-list">
        {query.isLoading && !stories.length ? (
          <PageState.Loading layout="panel" rows={8} label="loading News stories" />
        ) : null}
        {query.isError && !stories.length ? (
          <PageState.Error error={query.error ?? "News unavailable"} />
        ) : null}
        {query.isError && stories.length ? (
          <p className="news-degraded-state" role="status">
            最新更新失败，正在显示上一次成功读取的 Story。
          </p>
        ) : null}
        {!query.isLoading && !query.isError && !stories.length ? (
          <PageState.Empty title="暂无 Story" hint="当前筛选条件没有匹配的新闻事件。" />
        ) : null}
        {!query.isLoading && stories.length ? (
          <PageState.Stale updating={query.isFetching && !query.isLoading}>
            {stories.map((story) => (
              <StoryRow
                key={story.story_id}
                onOpen={() => navigate(newsStoryPath(story.story_id))}
                story={story}
              />
            ))}
          </PageState.Stale>
        ) : null}
      </div>
    </section>
  );
}

function StoryRow({ story, onOpen }: { story: NewsStorySummary; onOpen: () => void }) {
  return (
    <button className="news-story-row" onClick={onOpen} type="button">
      <span className="news-importance" data-level={importanceLevel(story.importance_score)}>
        <b>{story.importance_score}</b>
        <small>重要性</small>
      </span>
      <span className="news-story-copy">
        <span className="news-story-meta">
          <b>{phaseLabel(story.phase)}</b>
          <span data-verification={story.verification_status}>
            {verificationLabel(story.verification_status)}
          </span>
          <span>{story.primary_article.source_name}</span>
          <time>{relativeTime(story.last_seen_at_ms)}</time>
        </span>
        <strong>{story.title}</strong>
        <small>{story.short_conclusion || story.snippet || "暂无摘要"}</small>
      </span>
      <span className="news-story-counts">
        <b>{story.source_count}</b>
        <small>采集源</small>
        <b>{story.independent_origin_count}</b>
        <small>独立原始源</small>
        <span data-analysis={story.analysis_status}>{analysisLabel(story.analysis_status)}</span>
      </span>
    </button>
  );
}

function NewsStoryRoute({ token, storyId }: { token: string; storyId: string }) {
  const query = useNewsStoryWithToken(token, storyId);
  const story = query.data ?? null;
  return (
    <section
      aria-label="News Story detail"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <header className="radar-toolbar news-toolbar">
        <Link className="news-back-link" to={newsPath()}>
          <ArrowLeft aria-hidden />
          返回 Story 流
        </Link>
        <span className="news-live-state">{query.isFetching ? "更新中" : "实时"}</span>
      </header>
      {query.isLoading && !story ? (
        <PageState.Loading layout="panel" rows={8} label="loading News Story" />
      ) : null}
      {query.isError ? <PageState.Error error={query.error ?? "News Story unavailable"} /> : null}
      {!query.isLoading && !query.isError && !story ? (
        <PageState.Empty title="Story 不存在" />
      ) : null}
      {story ? (
        <PageState.Stale updating={query.isFetching}>
          <StoryDetail story={story} />
        </PageState.Stale>
      ) : null}
    </section>
  );
}

function StoryDetail({ story }: { story: NewsStoryDetail }) {
  const articleById = new Map(story.articles.map((article) => [article.article_id, article]));
  return (
    <article className="news-story-detail">
      <header className="news-story-hero">
        <div>
          <span className="news-eyebrow">
            <ShieldCheck aria-hidden />
            {verificationLabel(story.verification_status)} · {phaseLabel(story.phase)}
          </span>
          <h2>{story.title}</h2>
          <p>{story.snippet || "暂无事件摘要。"}</p>
        </div>
        <span className="news-detail-score">
          <b>{story.importance_score}</b>
          <small>重要性 / 100</small>
        </span>
      </header>

      <section className="news-evidence-metrics" aria-label="Story evidence metrics">
        <Metric label="采集源" value={story.source_count} />
        <Metric label="成员 Article" value={story.article_count} />
        <Metric label="权威/可信源" value={story.trusted_source_count} />
        <Metric label="独立原始源" value={story.independent_origin_count} />
      </section>

      <div className="news-detail-grid">
        <main>
          <AnalysisCard story={story} />
          <section className="news-detail-card">
            <SectionTitle icon={Radio} title="来源证据" />
            <div className="news-article-list">
              {story.articles.map((article) => (
                <ArticleEvidence
                  article={article}
                  key={article.article_id}
                  referenced={Boolean(
                    story.analysis?.evidence_references.includes(article.article_id),
                  )}
                />
              ))}
            </div>
          </section>
        </main>
        <aside>
          <section className="news-detail-card">
            <h3>Story 状态</h3>
            <dl>
              <Definition label="阶段" value={phaseLabel(story.phase)} />
              <Definition label="核验" value={verificationLabel(story.verification_status)} />
              <Definition label="首次发现" value={absoluteTime(story.first_seen_at_ms)} />
              <Definition label="最近更新" value={absoluteTime(story.last_seen_at_ms)} />
              <Definition label="AI" value={analysisLabel(story.analysis_status)} />
            </dl>
            {story.analysis_error ? (
              <p className="news-analysis-error">{story.analysis_error}</p>
            ) : null}
          </section>
          <section className="news-detail-card">
            <h3>重要性因素</h3>
            <dl>
              <Definition label="来源权威度" value={importanceFactor(story, "authority")} />
              <Definition
                label="独立交叉验证"
                value={importanceFactor(story, "independent_corroboration")}
              />
              <Definition label="时效性" value={importanceFactor(story, "recency")} />
              <Definition label="事件严重性" value={importanceFactor(story, "severity")} />
            </dl>
          </section>
          <section className="news-detail-card">
            <h3>成员归并审计</h3>
            <div className="news-membership-list">
              {story.memberships.map((membership) => (
                <div key={membership.article_id}>
                  <b>
                    {articleById.get(membership.article_id)?.source_name ?? membership.article_id}
                  </b>
                  <span>{membership.match_method}</span>
                  <small>{Math.round(membership.match_score * 100)}% 匹配</small>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </article>
  );
}

function AnalysisCard({ story }: { story: NewsStoryDetail }) {
  if (!story.analysis) {
    return (
      <section className="news-detail-card news-analysis-empty">
        <SectionTitle icon={Sparkles} title="DeepSeek 中文分析" />
        <h3>{analysisLabel(story.analysis_status)}</h3>
        <p>原始证据已经保留；AI 结论只有在对应证据集成功发布后才会显示。</p>
      </section>
    );
  }
  const analysis = story.analysis;
  return (
    <section className="news-detail-card news-analysis-card">
      <SectionTitle icon={Sparkles} title="DeepSeek 中文分析" />
      <AnalysisSection title="发生了什么" value={analysis.what_happened} />
      <AnalysisSection title="为什么重要" value={analysis.why_it_matters} />
      <div className="news-impact-grid">
        <AnalysisSection title="政治影响" value={analysis.political_impact} />
        <AnalysisSection title="经济与市场影响" value={analysis.economic_market_impact} />
      </div>
      <div className="news-impact-grid">
        <StringList title="已确认事实" values={analysis.confirmed_facts} />
        <StringList title="分歧与未知" values={analysis.disagreements_unknowns} />
      </div>
      <AnalysisSection title="下一检查点" value={analysis.next_checkpoint} />
      <footer>
        <span>{analysis.model}</span>
        <time>{absoluteTime(analysis.published_at_ms)}</time>
      </footer>
    </section>
  );
}

function ArticleEvidence({ article, referenced }: { article: NewsArticle; referenced: boolean }) {
  const url = article.origin_url || article.canonical_url;
  return (
    <article className="news-article-evidence">
      <header>
        <div>
          <b>{article.source_name}</b>
          <span>{article.source_role === "trusted_aggregator" ? "聚合源" : "原始媒体"}</span>
          <span>采集链：{article.source_chain_id}</span>
          <span data-provenance={article.provenance_status}>{article.provenance_status}</span>
          {referenced ? <span data-referenced="true">AI 引用</span> : null}
        </div>
        <time>{absoluteTime(article.published_at_ms)}</time>
      </header>
      <h3>{article.title}</h3>
      {article.snippet ? <p>{article.snippet}</p> : null}
      <footer>
        <span>
          原始出处：{article.origin_name || article.origin_domain || "未识别"}
          {article.origin_domain ? ` · ${article.origin_domain}` : ""}
        </span>
        {url ? (
          <a href={url} rel="noreferrer" target="_blank">
            查看原文
            <ExternalLink aria-hidden />
          </a>
        ) : null}
      </footer>
    </article>
  );
}

function StoryPager({
  hasNextPage,
  isFetching,
  pageNumber,
  rowCount,
  onNext,
  onPrevious,
}: {
  hasNextPage: boolean;
  isFetching: boolean;
  pageNumber: number;
  rowCount: number;
  onNext: () => void;
  onPrevious: () => void;
}) {
  return (
    <nav className="news-pager" aria-label="Story pagination">
      <button
        aria-label="Previous Story page"
        disabled={pageNumber <= 1 || isFetching}
        onClick={onPrevious}
        type="button"
      >
        <ChevronLeft aria-hidden />
      </button>
      <span>
        第 {pageNumber} 页 · {rowCount}/{NEWS_PAGE_SIZE}
      </span>
      <button
        aria-label="Next Story page"
        disabled={!hasNextPage || isFetching}
        onClick={onNext}
        type="button"
      >
        <ChevronRight aria-hidden />
      </button>
    </nav>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <b>{value}</b>
      <span>{label}</span>
    </div>
  );
}

function SectionTitle({ icon: Icon, title }: { icon: typeof Sparkles; title: string }) {
  return (
    <div className="news-section-title">
      <Icon aria-hidden />
      <h3>{title}</h3>
    </div>
  );
}

function AnalysisSection({ title, value }: { title: string; value: string }) {
  return (
    <div className="news-analysis-section">
      <h4>{title}</h4>
      <p>{value}</p>
    </div>
  );
}

function StringList({ title, values }: { title: string; values: string[] }) {
  return (
    <div className="news-analysis-section">
      <h4>{title}</h4>
      {values.length ? (
        <ul>
          {values.map((value) => (
            <li key={value}>{value}</li>
          ))}
        </ul>
      ) : (
        <p>无</p>
      )}
    </div>
  );
}

function Definition({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function importanceFactor(
  story: NewsStoryDetail,
  key: "authority" | "independent_corroboration" | "recency" | "severity",
): string {
  const value = story.importance_factors[key];
  return typeof value === "number" && Number.isFinite(value) ? `${value} 分` : "未知";
}

function normalizeVerification(value: string | null): NewsVerificationStatus | "all" {
  return VERIFICATION_FILTERS.includes(value as (typeof VERIFICATION_FILTERS)[number])
    ? (value as NewsVerificationStatus | "all")
    : "all";
}

function importanceLevel(score: number): string {
  if (score >= 75) return "critical";
  if (score >= 55) return "high";
  return "normal";
}

function relativeTime(value: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - value) / 1000));
  if (seconds < 60) return "刚刚";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function absoluteTime(value: number): string {
  return new Date(value).toLocaleString();
}
