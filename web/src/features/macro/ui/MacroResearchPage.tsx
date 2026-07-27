import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { CalendarDays, ExternalLink, FileWarning, Quote } from "lucide-react";
import { type FormEvent, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import { useSearchParams } from "react-router-dom";
import remarkGfm from "remark-gfm";

import { useMacroResearchQuery } from "../api/useMacroResearchQuery";
import type { MacroResearchCitationData, MacroResearchPublicationData } from "../model/macroTypes";

import "./MacroResearchPage.css";

const SESSION_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function MacroResearchPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSession = searchParams.get("session_date");
  const sessionDate =
    requestedSession && SESSION_DATE.test(requestedSession) ? requestedSession : null;
  const [draftSession, setDraftSession] = useState(sessionDate ?? "");
  const query = useMacroResearchQuery({ sessionDate, token });

  if (query.isError) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isLoading || !query.data) {
    return <PageState.Loading label="加载宏观研究" layout="route" />;
  }

  const data = query.data;

  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main
        aria-label="宏观研究工作台"
        className="macro-research-workbench"
        data-page-archetype="decision"
      >
        <header className="macro-research-header">
          <div>
            <span>DEEPAGENTS · PERSISTED RESEARCH</span>
            <h1>宏观研究工作台</h1>
            <p>读取已完成交易日的持久化研究；浏览器不会临时调用模型或重算结论。</p>
          </div>
          <SessionPicker
            draftSession={draftSession}
            onDraftChange={setDraftSession}
            onLatest={() => {
              setDraftSession("");
              setSearchParams({});
            }}
            onSubmit={(event) => {
              event.preventDefault();
              if (SESSION_DATE.test(draftSession)) {
                setSearchParams({ session_date: draftSession });
              }
            }}
          />
        </header>

        <ResearchStateBanner
          currentSession={data.current_session_date}
          requestedSession={data.requested_session_date}
          run={data.run}
          state={data.state}
        />

        {data.publication ? (
          <ResearchDocument publication={data.publication} state={data.state} />
        ) : data.state === "generating" ? (
          <PageState.Empty
            title="研究正在生成"
            hint="页面只轮询持久化状态；完成后将自动显示同一交易日的研究文档。"
          />
        ) : data.state === "failed" ? (
          <PageState.Empty
            title="本次研究生成失败"
            hint={data.run?.last_error ?? "运行失败原因尚未写入。"}
          />
        ) : (
          <PageState.Empty
            title="该交易日尚无宏观研究"
            hint="选择其他已完成交易日，或等待后台研究任务创建持久化结果。"
          />
        )}
      </main>
    </PageState.Stale>
  );
}

function SessionPicker({
  draftSession,
  onDraftChange,
  onLatest,
  onSubmit,
}: {
  draftSession: string;
  onDraftChange: (value: string) => void;
  onLatest: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form aria-label="选择研究交易日" className="macro-research-session-picker" onSubmit={onSubmit}>
      <label htmlFor="macro-research-session-date">已完成交易日</label>
      <div>
        <input
          id="macro-research-session-date"
          onChange={(event) => onDraftChange(event.target.value)}
          type="date"
          value={draftSession}
        />
        <Button disabled={!SESSION_DATE.test(draftSession)} size="sm" type="submit">
          读取
        </Button>
        <Button onClick={onLatest} size="sm" type="button" variant="outline">
          最新
        </Button>
      </div>
    </form>
  );
}

function ResearchStateBanner({
  currentSession,
  requestedSession,
  run,
  state,
}: {
  currentSession: string;
  requestedSession: string;
  run: { attempt_count: number; max_attempts: number; status: string } | null;
  state: "current" | "historical" | "generating" | "failed" | "missing";
}) {
  const stateLabel = {
    current: "当前研究",
    failed: "生成失败",
    generating: "正在生成",
    historical: "历史研究",
    missing: "尚未生成",
  }[state];
  return (
    <section aria-label="研究状态" className="macro-research-state" data-state={state}>
      <CalendarDays aria-hidden="true" />
      <div>
        <strong>{stateLabel}</strong>
        <span>
          请求交易日 {requestedSession} · 当前完成交易日 {currentSession}
        </span>
      </div>
      {run ? (
        <small>
          {run.status} · 尝试 {run.attempt_count}/{run.max_attempts}
        </small>
      ) : null}
    </section>
  );
}

function ResearchDocument({
  publication,
  state,
}: {
  publication: MacroResearchPublicationData;
  state: "current" | "historical" | "generating" | "failed" | "missing";
}) {
  return (
    <article aria-labelledby="macro-research-title" className="macro-research-document">
      <header className="macro-research-document-header">
        <div>
          <span>
            {state === "current" ? "CURRENT" : "HISTORICAL"} · {publication.session_date}
          </span>
          <h2 id="macro-research-title">{publication.title}</h2>
          <p>{publication.executive_summary}</p>
        </div>
        <dl>
          <div>
            <dt>市场截止</dt>
            <dd>{formatInstant(publication.market_cutoff_ms)}</dd>
          </div>
          <div>
            <dt>文档版本</dt>
            <dd>{publication.schema_version}</dd>
          </div>
          <div>
            <dt>Evidence Pack</dt>
            <dd>{publication.evidence_pack_id}</dd>
          </div>
          <div>
            <dt>Reviewer</dt>
            <dd>{publication.reviewer_disposition}</dd>
          </div>
        </dl>
      </header>

      <div className="macro-research-sections">
        {publication.sections.map((section) => (
          <section aria-labelledby={`macro-section-${section.section_id}`} key={section.section_id}>
            <h3 id={`macro-section-${section.section_id}`}>{section.title}</h3>
            <ResearchMarkdown source={section.body_markdown} />
            {section.citation_ids.length ? (
              <p className="macro-research-inline-citations">
                引用：{section.citation_ids.map((citationId) => `[${citationId}]`).join(" ")}
              </p>
            ) : null}
          </section>
        ))}
      </div>

      <section aria-labelledby="macro-research-gaps" className="macro-research-gaps">
        <header>
          <FileWarning aria-hidden="true" />
          <h3 id="macro-research-gaps">证据缺口与开放问题</h3>
        </header>
        {publication.evidence_gaps.length ? (
          <ul>
            {publication.evidence_gaps.map((gap) => (
              <li key={gap.gap_id}>
                <strong>{gap.summary}</strong>
                {gap.details ? <p>{gap.details}</p> : null}
                {gap.citation_ids.length ? <small>{gap.citation_ids.join(" · ")}</small> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p>Agent 未声明额外证据缺口。</p>
        )}
      </section>

      <section aria-labelledby="macro-research-citations" className="macro-research-citations">
        <header>
          <Quote aria-hidden="true" />
          <h3 id="macro-research-citations">引用与事实溯源</h3>
        </header>
        <ol>
          {publication.citations.map((citation) => (
            <CitationItem citation={citation} key={citation.citation_id} />
          ))}
        </ol>
      </section>

      <details className="macro-research-audit">
        <summary>审阅与运行审计</summary>
        <div>
          <h3>Reviewer notes</h3>
          {publication.reviewer_notes.length ? (
            <ul>
              {publication.reviewer_notes.map((note, index) => (
                <li key={`${index}:${note}`}>{note}</li>
              ))}
            </ul>
          ) : (
            <p>无额外 reviewer note。</p>
          )}
          <h3>Audit</h3>
          <pre>{JSON.stringify(publication.audit, null, 2)}</pre>
        </div>
      </details>
    </article>
  );
}

function ResearchMarkdown({ source }: { source: string }) {
  return (
    <div className="macro-research-markdown">
      <Markdown components={RESEARCH_MARKDOWN_COMPONENTS} remarkPlugins={[remarkGfm]} skipHtml>
        {source}
      </Markdown>
    </div>
  );
}

const RESEARCH_MARKDOWN_COMPONENTS = {
  a({ node: _node, href, children, ...props }) {
    const isExternal = href ? isExternalMarkdownHref(href) : false;
    return (
      <a
        {...props}
        href={href || undefined}
        rel={isExternal ? "noreferrer noopener" : undefined}
        target={isExternal ? "_blank" : undefined}
      >
        {children}
      </a>
    );
  },
} satisfies Components;

function isExternalMarkdownHref(href: string): boolean {
  return href.startsWith("//") || /^[A-Za-z][A-Za-z\d+.-]*:/.test(href);
}

function CitationItem({ citation }: { citation: MacroResearchCitationData }) {
  return (
    <li id={`citation-${citation.citation_id}`}>
      <span>{citation.citation_id}</span>
      <div>
        <strong>{citation.source_label}</strong>
        <small>
          {citation.source_type} · {citation.observed_at ?? "无观察日期"} ·{" "}
          {citation.available_at_ms
            ? `截止前可用 ${formatInstant(citation.available_at_ms)}`
            : "可用时间未记录"}
        </small>
        <code>{citation.source_ref}</code>
      </div>
      {citation.source_url ? (
        <a href={citation.source_url} rel="noreferrer" target="_blank">
          来源
          <ExternalLink aria-hidden="true" />
        </a>
      ) : null}
    </li>
  );
}

function formatInstant(value: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
