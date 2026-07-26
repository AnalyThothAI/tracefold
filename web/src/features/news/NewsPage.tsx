import { newsBriefPath, newsPath, newsStoryPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Newspaper,
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
  evidencePostureLabel,
  lifecycleLabel,
  type NewsEvidencePosture,
  type NewsStoryDetail,
  type NewsStorySummary,
} from "./model/newsStoryViewModel";
import {
  NEWS_PAGE_SIZE,
  useNewsBriefHistoryWithToken,
  useNewsBriefWithToken,
  useNewsStoriesWithToken,
  useNewsStoryWithToken,
} from "./useNewsPage";

type NewsPageProps = {
  brief?: boolean;
  storyId?: string | null;
  token: string;
};

type CursorState = { queryKey: string; stack: Array<string | null> };
type JsonRecord = Record<string, unknown>;

const EMPTY_STORIES: NewsStorySummary[] = [];
const POSTURE_FILTERS = [
  "all",
  "primary_source_confirmed",
  "independently_corroborated",
  "single_origin_reported",
  "contested",
  "corrected",
] as const;

export function NewsPage({ brief = false, token, storyId = null }: NewsPageProps) {
  if (brief) return <GlobalBriefRoute token={token} />;
  if (storyId) return <NewsStoryRoute storyId={storyId} token={token} />;
  return <NewsStoryListRoute token={token} />;
}

function NewsStoryListRoute({ token }: { token: string }) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const searchQuery = searchParams.get("q") ?? "";
  const sourceQuery = searchParams.get("source") ?? "";
  const posture = normalizePosture(searchParams.get("posture"));
  const queryKey = `${searchQuery}\n${sourceQuery}\n${posture}`;
  const [cursorState, setCursorState] = useState<CursorState>(() => ({
    queryKey,
    stack: [null],
  }));
  const cursorStack = cursorState.queryKey === queryKey ? cursorState.stack : [null];
  const cursor = cursorStack.at(-1) ?? null;
  const query = useNewsStoriesWithToken(token, {
    cursor,
    evidencePosture: posture === "all" ? null : posture,
    q: searchQuery.trim() || null,
    source: sourceQuery.trim() || null,
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
          <h2>全球政治经济事件流</h2>
          <p>Story 只代表一次现实状态变化；证据姿态、影响与当前优先级分开呈现。</p>
          <Link className="news-back-link" to={newsBriefPath()}>
            <Newspaper aria-hidden />
            查看 Global Brief
          </Link>
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
              return { queryKey, stack: stack.length > 1 ? stack.slice(0, -1) : stack };
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
            placeholder="Reuters、官方机构…"
            value={sourceQuery}
          />
        </label>
        <div className="news-verification-filters" aria-label="Evidence posture filters">
          {POSTURE_FILTERS.map((value) => (
            <button
              aria-pressed={posture === value}
              key={value}
              onClick={() => updateParam("posture", value)}
              type="button"
            >
              {value === "all" ? "全部" : evidencePostureLabel(value)}
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
        {!query.isLoading && !query.isError && !stories.length ? (
          <PageState.Empty title="暂无 Story" hint="当前筛选条件没有匹配的事件。" />
        ) : null}
        {stories.length ? (
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
      <span className="news-importance" data-level={scoreLevel(story.impact_score)}>
        <b>{story.impact_score}</b>
        <small>影响</small>
      </span>
      <span className="news-story-copy">
        <span className="news-story-meta">
          <b>{lifecycleLabel(story.lifecycle)}</b>
          <span data-verification={story.evidence_posture}>
            {evidencePostureLabel(story.evidence_posture)}
          </span>
          <span>{story.representative_evidence.source_name}</span>
          <time>{relativeTime(story.last_material_evidence_at_ms)}</time>
        </span>
        <strong>{story.title}</strong>
        <small>{story.analysis.short_conclusion || story.snippet || "暂无摘要"}</small>
      </span>
      <span className="news-story-counts">
        <b>{story.priority_score}</b>
        <small>当前优先级</small>
        <b>{story.independent_origin_count}</b>
        <small>独立原始源</small>
        <span data-analysis={story.analysis.status}>{analysisLabel(story.analysis.status)}</span>
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
      {story ? <StoryDetail story={story} /> : null}
    </section>
  );
}

function StoryDetail({ story }: { story: NewsStoryDetail }) {
  const analysis = record(story.analysis);
  const currentPublication = record(analysis.current);
  const analysisPayload = record(currentPublication.payload);
  return (
    <article className="news-story-detail">
      <header className="news-story-hero">
        <div>
          <span className="news-eyebrow">
            <ShieldCheck aria-hidden />
            {evidencePostureLabel(story.evidence_posture)} · {lifecycleLabel(story.lifecycle)}
          </span>
          <h2>{story.title}</h2>
          <p>{story.snippet || "暂无事件摘要。"}</p>
        </div>
        <span className="news-detail-score">
          <b>{story.impact_score}</b>
          <small>影响 / 100</small>
        </span>
      </header>
      <section className="news-evidence-metrics" aria-label="Story evidence metrics">
        <Metric label="当前优先级" value={story.priority_score} />
        <Metric label="主要成员" value={story.primary_member_count} />
        <Metric label="上下文成员" value={story.contextual_member_count} />
        <Metric label="独立原始源" value={story.independent_origin_count} />
      </section>
      <div className="news-detail-grid">
        <main>
          <StoryAnalysis status={text(analysis.status)} payload={analysisPayload} />
          <section className="news-detail-card">
            <SectionTitle icon={Radio} title="Article 与 Revision 证据" />
            <div className="news-article-list">
              {story.articles.map((article, index) => (
                <ArticleEvidence article={article} key={`${text(article.revision_id)}-${index}`} />
              ))}
            </div>
          </section>
          <AuditCard title="身份判定" rows={story.identity_decisions} />
          <AuditCard title="材料事件" rows={story.material_events} />
          <AuditCard title="Brief 选材判定" rows={story.selection_audit} />
        </main>
        <aside>
          <section className="news-detail-card">
            <h3>Story 状态</h3>
            <dl>
              <Definition label="材料变化" value={story.material_evolution_state} />
              <Definition label="证据姿态" value={evidencePostureLabel(story.evidence_posture)} />
              <Definition label="首次发现" value={absoluteTime(story.first_seen_at_ms)} />
              <Definition
                label="最后材料证据"
                value={absoluteTime(story.last_material_evidence_at_ms)}
              />
              <Definition label="AI" value={analysisLabel(text(analysis.status))} />
            </dl>
          </section>
          <FactorCard title="影响因素" factors={story.impact_profile} />
          <FactorCard title="优先级因素" factors={story.priority_profile} />
          <section className="news-detail-card">
            <h3>成员语义</h3>
            <div className="news-membership-list">
              {story.memberships.map((membership, index) => (
                <div key={`${text(membership.article_id)}-${index}`}>
                  <b>{text(membership.content_form, "unknown")}</b>
                  <span>{text(membership.origin_relation, "unresolved")}</span>
                  <small>
                    {text(membership.development_relation)} · {text(membership.epistemic_use)}
                  </small>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </article>
  );
}

function StoryAnalysis({ status, payload }: { status: string; payload: JsonRecord }) {
  if (!Object.keys(payload).length) {
    return (
      <section className="news-detail-card news-analysis-empty">
        <SectionTitle icon={Sparkles} title="中文 Story Analysis" />
        <h3>{analysisLabel(status)}</h3>
        <p>事实证据始终可读；只有通过证据引用和落地校验的分析才会发布。</p>
      </section>
    );
  }
  return (
    <section className="news-detail-card news-analysis-card">
      <SectionTitle icon={Sparkles} title="中文 Story Analysis" />
      <AnalysisSection title="发生了什么" value={factText(payload.what_happened)} />
      <AnalysisSection title="为什么重要" value={text(payload.why_it_matters)} />
      <div className="news-impact-grid">
        <AnalysisSection title="政治影响" value={text(payload.political_impact)} />
        <AnalysisSection title="经济与市场影响" value={text(payload.economic_market_impact)} />
      </div>
      <StringList title="分歧与未知" values={strings(payload.disagreements_unknowns)} />
      <AnalysisSection title="下一检查点" value={text(payload.next_checkpoint)} />
    </section>
  );
}

function GlobalBriefRoute({ token }: { token: string }) {
  const query = useNewsBriefWithToken(token);
  const history = useNewsBriefHistoryWithToken(token);
  const data = query.data;
  const publication = data?.current ?? null;
  const payload = record(publication?.payload);
  const fallback = record(data?.fallback);
  const fallbackBundle = record(fallback.evidence_bundle);
  const fallbackStories = records(fallbackBundle.stories);
  return (
    <section
      aria-label="Global Brief"
      className="radar-panel news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <header className="radar-toolbar news-toolbar">
        <Link className="news-back-link" to={newsPath()}>
          <ArrowLeft aria-hidden />
          返回 Story 流
        </Link>
        <span className="news-live-state">{query.isFetching ? "更新中" : "已验证发布"}</span>
      </header>
      {query.isLoading ? (
        <PageState.Loading layout="panel" rows={8} label="loading Global Brief" />
      ) : null}
      {query.isError ? <PageState.Error error={query.error ?? "Global Brief unavailable"} /> : null}
      {publication ? (
        <article className="news-story-detail">
          <header className="news-story-hero">
            <div>
              <span className="news-eyebrow">
                <Newspaper aria-hidden />
                Global Brief · {absoluteTime(publication.cutoff_at_ms)}
              </span>
              <h2>{text(payload.headline, "全球政治经济简报")}</h2>
              <p>{text(payload.executive_summary)}</p>
            </div>
          </header>
          <section className="news-article-list">
            {records(payload.items).map((item, index) => (
              <BriefItem item={item} key={`${text(item.story_id)}-${index}`} />
            ))}
          </section>
        </article>
      ) : fallbackStories.length ? (
        <section className="news-detail-card news-analysis-empty">
          <SectionTitle icon={ShieldCheck} title="确定性 Brief 选材" />
          <p>尚无通过校验的 AI Brief。以下是冻结选材，不使用生成文本补位。</p>
          {fallbackStories.map((story) => (
            <Link key={text(story.story_id)} to={newsStoryPath(text(story.story_id))}>
              {text(story.title)}
            </Link>
          ))}
        </section>
      ) : (
        <PageState.Empty title="暂无 Global Brief" hint="尚没有达到选材门槛的 Story。" />
      )}
      {data?.latest_failure ? (
        <p className="news-degraded-state" role="status">
          最近一次生成失败；当前页面保留上一份有效发布或确定性选材。
        </p>
      ) : null}
      {history.data?.items.length ? (
        <section className="news-detail-card">
          <h3>历史发布</h3>
          <ul>
            {history.data.items.map((item) => (
              <li key={item.publication_id}>
                {absoluteTime(item.cutoff_at_ms)} · {text(item.contract.model, "unknown model")}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}

function BriefItem({ item }: { item: JsonRecord }) {
  return (
    <article className="news-article-evidence">
      <header>
        <b>{factText(item.what_happened)}</b>
      </header>
      <p>{text(item.why_it_matters)}</p>
      <p>{transmissionText(item.transmission_scenarios)}</p>
      <footer>
        <Link to={newsStoryPath(text(item.story_id))}>查看 Story 证据</Link>
      </footer>
    </article>
  );
}

function ArticleEvidence({ article }: { article: JsonRecord }) {
  const url = text(article.canonical_url);
  return (
    <article className="news-article-evidence">
      <header>
        <div>
          <b>{text(article.source_name, text(article.source_id, "未知来源"))}</b>
          <span>{text(article.source_role)}</span>
          <span>{text(article.origin_relation, "unresolved")}</span>
          <span>{text(article.epistemic_use)}</span>
        </div>
        <time>{absoluteTime(number(article.source_published_at_ms))}</time>
      </header>
      <h3>{text(article.title)}</h3>
      {text(article.snippet) ? <p>{text(article.snippet)}</p> : null}
      <footer>
        <span>
          Revision {number(article.revision_number)} · {text(article.material_change_kind)}
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

function AuditCard({ title, rows }: { title: string; rows: JsonRecord[] }) {
  if (!rows.length) return null;
  return (
    <section className="news-detail-card">
      <h3>{title}</h3>
      <pre>{JSON.stringify(rows, null, 2)}</pre>
    </section>
  );
}

function FactorCard({ title, factors }: { title: string; factors: JsonRecord }) {
  return (
    <section className="news-detail-card">
      <h3>{title}</h3>
      <dl>
        {Object.entries(factors).map(([key, value]) => (
          <Definition key={key} label={key} value={formatValue(value)} />
        ))}
      </dl>
    </section>
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
      <p>{value || "未知"}</p>
    </div>
  );
}

function StringList({ title, values }: { title: string; values: string[] }) {
  return (
    <div className="news-analysis-section">
      <h4>{title}</h4>
      {values.length ? (
        <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul>
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

function normalizePosture(value: string | null): NewsEvidencePosture | "all" {
  return POSTURE_FILTERS.includes(value as (typeof POSTURE_FILTERS)[number])
    ? (value as NewsEvidencePosture | "all")
    : "all";
}

function scoreLevel(score: number): string {
  return score >= 85 ? "critical" : score >= 65 ? "high" : "normal";
}

function relativeTime(value: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() - value) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1_440) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 1_440)} 天前`;
}

function absoluteTime(value: number): string {
  return value > 0 ? new Date(value).toLocaleString() : "未知";
}

function record(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function records(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function formatValue(value: unknown): string {
  if (typeof value === "number") return String(Math.round(value * 100) / 100);
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function factText(value: unknown): string {
  return records(value)
    .map((fact) => text(fact.text))
    .filter(Boolean)
    .join(" ");
}

function transmissionText(value: unknown): string {
  return records(value)
    .map((scenario) =>
      [text(scenario.condition), text(scenario.mechanism), text(scenario.possible_effect)]
        .filter(Boolean)
        .join("；"),
    )
    .filter(Boolean)
    .join(" ");
}
