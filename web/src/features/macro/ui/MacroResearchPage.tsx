import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { CalendarDays } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useMacroResearchQuery } from "../api/useMacroResearchQuery";
import type {
  MacroLiveDeltaV1,
  MacroOutcomeReplayV1,
  MacroThesisDetailReadData,
  MacroThesisV1,
} from "../model/macroTypes";

import "./MacroResearchPage.css";

const SESSION_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function MacroResearchPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSession = searchParams.get("session_date");
  const sessionDate =
    requestedSession && SESSION_DATE.test(requestedSession) ? requestedSession : null;
  const [draftSession, setDraftSession] = useState(sessionDate ?? "");
  const query = useMacroResearchQuery({ sessionDate, token });

  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isLoading || !query.data) {
    return <PageState.Loading label="加载 Macro Thesis 档案" layout="route" />;
  }
  const data = query.data;

  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main
        aria-label="Macro Thesis 档案"
        className="macro-research-workbench"
        data-page-archetype="decision"
      >
        <header className="macro-research-header">
          <div>
            <span>DEEPAGENT · IMMUTABLE MACRO THESIS</span>
            <h1>Macro Thesis 档案</h1>
            <p>这里展示与总览完全相同的主线、独立审阅、Live Delta 和结果复盘。</p>
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

        <ThesisStateBanner data={data} transportStale={query.isError} />

        {data.thesis ? (
          <ThesisDocument
            liveDelta={data.live_delta}
            outcomeReplay={data.outcome_replay}
            state={data.state}
            thesis={data.thesis}
          />
        ) : data.state === "generating" ? (
          <PageState.Empty
            title="Macro Thesis 正在生成"
            hint="页面只轮询持久化状态，不会在浏览器中启动或恢复 Agent。"
          />
        ) : data.state === "failed" || data.state === "not_published" ? (
          <PageState.Empty
            title="本次 Macro Thesis 未发布"
            hint={data.run?.error_message ?? data.run?.error_code ?? "失败原因尚未写入。"}
          />
        ) : (
          <PageState.Empty
            title="该交易日尚无 Macro Thesis"
            hint="选择其他交易日，或等待 08:50 New York 后台任务完成。"
          />
        )}

        <PublicationHistory history={data.history} />
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
    <form
      aria-label="选择 Thesis 交易日"
      className="macro-research-session-picker"
      onSubmit={onSubmit}
    >
      <label htmlFor="macro-research-session-date">交易日</label>
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

function ThesisStateBanner({
  data,
  transportStale,
}: {
  data: MacroThesisDetailReadData;
  transportStale: boolean;
}) {
  const stateLabel = {
    current: "当前主线",
    failed: "发布失败",
    generating: "正在生成",
    historical: "历史主线",
    missing: "尚未生成",
    not_published: "审阅未通过，未发布",
  }[data.state];
  return (
    <section aria-label="主线状态" className="macro-research-state" data-state={data.state}>
      <CalendarDays aria-hidden="true" />
      <div>
        <strong>{stateLabel}</strong>
        <span>
          请求 {data.requested_session_date} · 当前 {data.current_session_date} · 传输{" "}
          {transportStale ? "陈旧缓存" : "当前读取"}
        </span>
      </div>
      {data.run ? (
        <small>
          {data.run.status} · 尝试 {data.run.attempt_count}/{data.run.max_attempts}
        </small>
      ) : null}
    </section>
  );
}

function ThesisDocument({
  thesis,
  liveDelta,
  outcomeReplay,
  state,
}: {
  thesis: MacroThesisV1;
  liveDelta: MacroLiveDeltaV1 | null;
  outcomeReplay: MacroOutcomeReplayV1 | null;
  state: MacroThesisDetailReadData["state"];
}) {
  return (
    <article aria-labelledby="macro-research-title" className="macro-research-document">
      <header className="macro-research-document-header">
        <div>
          <span>
            {state === "current" ? "CURRENT" : "HISTORICAL"} · {thesis.session_date}
          </span>
          <h2 id="macro-research-title">{thesis.mainline.title}</h2>
          <p>{thesis.mainline.thesis}</p>
        </div>
        <dl>
          <div>
            <dt>市场截止</dt>
            <dd>{formatInstant(thesis.cutoff_ms)}</dd>
          </div>
          <div>
            <dt>Evidence Pack</dt>
            <dd>{thesis.evidence_pack_id}</dd>
          </div>
          <div>
            <dt>主线</dt>
            <dd>
              {thesis.mainline.stance} · {thesis.mainline.horizon}
            </dd>
          </div>
          <div>
            <dt>独立 Reviewer</dt>
            <dd>
              {thesis.review.disposition} · {thesis.review.model_name}
            </dd>
          </div>
        </dl>
      </header>

      <div className="macro-research-sections">
        <section>
          <h3>主线论点与因果链</h3>
          {thesis.mainline.claims.map((claim) => (
            <article key={claim.claim_id}>
              <strong>{claim.statement}</strong>
              {claim.causal_edges.map((edge) => (
                <p key={`${claim.claim_id}:${edge.source}:${edge.target}`}>
                  {edge.source} → {edge.mechanism} → {edge.target}
                </p>
              ))}
              <small>{claim.supporting_evidence_refs.join(" · ")}</small>
            </article>
          ))}
        </section>
        {thesis.alternative_explanation ? (
          <section>
            <h3>唯一备选路径：{thesis.alternative_explanation.title}</h3>
            <p>{thesis.alternative_explanation.thesis}</p>
            <small>
              触发：
              {thesis.alternative_explanation.trigger_conditions
                .map((condition) => condition.condition_id)
                .join(" · ")}
            </small>
          </section>
        ) : null}
        <section>
          <h3>核心矛盾</h3>
          <ol>
            {thesis.core_tensions.map((tension) => (
              <li key={tension.tension_id}>
                <strong>{tension.statement}</strong>
                <p>
                  {tension.side_a.label}：{tension.side_a.statement}
                </p>
                <p>
                  {tension.side_b.label}：{tension.side_b.statement}
                </p>
                <small>
                  领先 {tension.leading_side} · 滞后 {tension.lagging_signal} ·{" "}
                  {tension.unresolved_reason}
                </small>
              </li>
            ))}
          </ol>
        </section>
        <section>
          <h3>相较上一期的变化</h3>
          <ol>
            {thesis.changes_from_prior.length ? (
              thesis.changes_from_prior.map((change) => (
                <li key={change.change_id}>
                  <strong>{change.status}</strong> · {change.statement}
                </li>
              ))
            ) : (
              <li>首期发布或没有足以改写主线的变化。</li>
            )}
          </ol>
        </section>
        <section>
          <h3>十二资产：动量与条件展望</h3>
          <div className="macro-research-assets" role="table">
            {thesis.assets.map((asset) => (
              <div key={asset.symbol} role="row">
                <strong role="cell">{asset.symbol}</strong>
                <span role="cell">
                  1W {asset.momentum.momentum_1w} · {formatSigned(asset.momentum.return_1w_pct)}
                </span>
                <span role="cell">
                  {asset.outlook_1w.direction} · {asset.outlook_1w.causal_channel}
                </span>
                <span role="cell">
                  1M {asset.momentum.momentum_1m} · {formatSigned(asset.momentum.return_1m_pct)}
                </span>
                <span role="cell">
                  {asset.outlook_1m.direction} · {asset.outlook_1m.causal_channel}
                </span>
              </div>
            ))}
          </div>
        </section>
        <section>
          <h3>Live Delta、失效条件与检查点</h3>
          <p>
            Live Delta：{liveDelta?.status ?? "尚未评估"}；匹配失效条件：
            {liveDelta?.matched_falsifier_ids.join(" · ") || "—"}
          </p>
          <ConditionList title="失效条件" rows={thesis.mainline.falsifiers} />
          <ConditionList title="检查点" rows={thesis.mainline.checkpoints} />
        </section>
        <section>
          <h3>六模块角色</h3>
          {thesis.module_assessments.map((role) => (
            <article key={role.module_id}>
              <strong>
                {role.module_id} · {role.role}
              </strong>
              <p>{role.analysis}</p>
            </article>
          ))}
        </section>
        <section>
          <h3>自适应研究叙事</h3>
          {thesis.narrative_sections.map((section) => (
            <article key={section.section_id}>
              <strong>{section.title}</strong>
              <p>{section.markdown}</p>
              <small>{section.evidence_refs.join(" · ")}</small>
            </article>
          ))}
        </section>
        <section>
          <h3>证据引用与真实缺口</h3>
          <p>
            {thesis.citations.length} 条闭合引用 · {thesis.gaps.length} 个数据缺口
          </p>
          <ul>
            {thesis.gaps.map((gap) => (
              <li key={gap.gap_id}>
                {gap.dataset_id} · {gap.axis}/{gap.state}：{gap.reason}
              </li>
            ))}
          </ul>
        </section>
        <section>
          <h3>Outcome Replay</h3>
          <ul>
            {outcomeReplay?.horizons.map((horizon) => (
              <li key={horizon.horizon}>
                {horizon.horizon} · {horizon.status} ·{" "}
                {horizon.realized_return_pct == null
                  ? "等待到期"
                  : `${formatSigned(horizon.realized_return_pct)}%`}
              </li>
            )) ?? <li>尚未创建复盘。</li>}
          </ul>
        </section>
      </div>

      <details className="macro-research-audit">
        <summary>独立审阅与运行审计</summary>
        <div>
          <h3>Reviewer findings</h3>
          <ul>
            {thesis.review.findings.map((finding) => (
              <li key={finding}>{finding}</li>
            ))}
          </ul>
          <h3>Provenance</h3>
          <dl className="macro-research-provenance">
            <div>
              <dt>Workflow</dt>
              <dd>{thesis.provenance.workflow_version}</dd>
            </div>
            <div>
              <dt>Research model</dt>
              <dd>{thesis.provenance.research_model}</dd>
            </div>
            <div>
              <dt>Research prompt</dt>
              <dd>{thesis.provenance.research_prompt_version}</dd>
            </div>
            <div>
              <dt>Research invocation</dt>
              <dd>{thesis.provenance.research_invocation_id}</dd>
            </div>
            <div>
              <dt>Reviewer model</dt>
              <dd>{thesis.provenance.reviewer_model}</dd>
            </div>
            <div>
              <dt>Reviewer prompt</dt>
              <dd>{thesis.provenance.reviewer_prompt_version}</dd>
            </div>
            <div>
              <dt>Draft hash</dt>
              <dd>{thesis.provenance.draft_hash}</dd>
            </div>
          </dl>
        </div>
      </details>
    </article>
  );
}

function ConditionList({
  title,
  rows,
}: {
  title: string;
  rows: MacroThesisV1["mainline"]["falsifiers"];
}) {
  return (
    <div>
      <h4>{title}</h4>
      <ul>
        {rows.map((row) => (
          <li key={row.condition_id}>
            {row.dataset_id}.{row.metric_name} {row.operator} {row.threshold}：{row.rationale}
          </li>
        ))}
      </ul>
    </div>
  );
}

function PublicationHistory({ history }: { history: MacroThesisDetailReadData["history"] }) {
  return (
    <section className="macro-research-citations">
      <header>
        <h3>不可变主线历史</h3>
      </header>
      <ol>
        {history.map((item) => (
          <li key={item.publication_id}>
            <span>{item.session_date}</span>
            <div>
              <strong>{item.title}</strong>
              <small>
                {item.stance} · {item.confidence} · {item.horizon}
              </small>
            </div>
            <Link to={`/macro/research?session_date=${item.session_date}`}>读取</Link>
          </li>
        ))}
      </ol>
    </section>
  );
}

function formatInstant(value: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatSigned(value: number | null): string {
  if (value == null) return "—";
  const rendered = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
  return `${value > 0 ? "+" : ""}${rendered}`;
}
