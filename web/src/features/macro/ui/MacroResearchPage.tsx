import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { ArrowLeft } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useMacroResearchQuery } from "../api/useMacroResearchQuery";
import {
  confidenceLabel,
  formatInstant,
  horizonLabel,
  runErrorLabel,
  runStatusLabel,
  stanceLabel,
} from "../model/macroPresentation";
import type { MacroReason, MacroThesisDetailReadData } from "../model/macroTypes";

import { StatusGlyph } from "./MacroResearchAppendices";
import { MacroResearchDossier } from "./MacroResearchDossier";

import "./MacroResearchAudit.css";
import "./MacroResearchDossier.css";
import "./MacroResearchPage.css";

const SESSION_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function MacroResearchPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSession = searchParams.get("session_date");
  const sessionDate =
    requestedSession && SESSION_DATE.test(requestedSession) ? requestedSession : null;
  const [draftSession, setDraftSession] = useState(sessionDate ?? "");
  const query = useMacroResearchQuery({ sessionDate, token });

  useEffect(() => {
    setDraftSession(sessionDate ?? "");
  }, [sessionDate]);

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
            <Link className="macro-research-back" to="/macro">
              <ArrowLeft aria-hidden="true" />
              返回主线总览
            </Link>
            <span>DEEPAGENT · IMMUTABLE MACRO THESIS</span>
            <h1>Macro Thesis 档案</h1>
            <p>按论点阅读不可变主线的完整论证链；当前事实不会改写历史判断。</p>
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

        <ThesisStateBanner
          data={data}
          lastSuccessfulReadAtMs={query.dataUpdatedAt || null}
          transportStale={query.isError}
        />
        {data.thesis ? (
          <MacroResearchDossier
            alternative={data.alternative_presentation}
            appendix={data.appendix}
            assets={data.asset_presentation}
            claims={data.claim_presentation}
            liveDelta={data.live_delta}
            mainline={data.mainline_presentation!}
            outcomeReplay={data.outcome_replay}
            state={data.state}
            thesis={data.thesis}
          />
        ) : data.state === "generating" ? (
          <PageState.Empty
            title="Macro Thesis 正在生成"
            hint={reasonText(data, "页面只轮询持久化状态，不会在浏览器中启动或恢复 Agent。")}
          />
        ) : data.state === "failed" || data.state === "not_published" ? (
          <PageState.Empty
            title="本次 Macro Thesis 未发布"
            hint={reasonText(data, "失败原因尚未写入。")}
          />
        ) : (
          <PageState.Empty
            title="该交易日尚无 Macro Thesis"
            hint={reasonText(data, "选择其他交易日，或等待 08:50 New York 后台任务完成。")}
          />
        )}

        <PublicationHistory history={data.history} requestedSession={data.requested_session_date} />
        <RunAttemptPanel requestedSession={data.requested_session_date} run={data.run} />
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
  lastSuccessfulReadAtMs,
}: {
  data: MacroThesisDetailReadData;
  transportStale: boolean;
  lastSuccessfulReadAtMs: number | null;
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
      <StatusGlyph tone={thesisStateTone(data.state)} />
      <div>
        <strong>
          {data.fallback.state === "available"
            ? `请求交易日未发布，展示 ${data.displayed_session_date} 已发布档案`
            : stateLabel}
        </strong>
        <span>
          请求交易日 {data.requested_session_date} · 当前已完成交易日 {data.current_session_date} ·
          实际展示 {data.displayed_session_date ?? "无已发布档案"} · 传输{" "}
          {transportStale ? "陈旧缓存" : "当前读取"}
        </span>
        {transportStale ? (
          <small>
            最后成功读取 {formatInstant(lastSuccessfulReadAtMs)} · 展示主线市场截止{" "}
            {formatInstant(data.thesis?.cutoff_ms ?? null)}
          </small>
        ) : null}
        {data.reason ? (
          <small>
            {data.reason.message}；影响：
            {reasonImpactLabel(data.reason.impact)}
            {data.reason.next_action ? `；恢复动作：${data.reason.next_action}` : ""}
            {data.reason.next_check_at_ms
              ? `；下次检查：${formatInstant(data.reason.next_check_at_ms)}`
              : ""}
          </small>
        ) : null}
      </div>
    </section>
  );
}

function RunAttemptPanel({
  run,
  requestedSession,
}: {
  run: MacroThesisDetailReadData["run"];
  requestedSession: string;
}) {
  return (
    <section aria-labelledby="macro-research-run-title" className="macro-research-run">
      <div>
        <span>PUBLICATION ATTEMPTS</span>
        <h2 id="macro-research-run-title">生成尝试（不属于档案历史）</h2>
      </div>
      {!run ? (
        <p role="status">{requestedSession} 没有持久化生成尝试。</p>
      ) : (
        <>
          <dl>
            <div>
              <dt>状态</dt>
              <dd>
                <StatusGlyph tone={runStatusTone(run.status)} />
                {runReaderStatus(run.status)}
              </dd>
            </div>
            <div>
              <dt>尝试</dt>
              <dd>
                {run.attempt_count}/{run.max_attempts}
              </dd>
            </div>
            <div>
              <dt>最后状态变化</dt>
              <dd>{formatInstant(run.updated_at_ms)}</dd>
            </div>
            <div>
              <dt>可重试</dt>
              <dd>{run.reason ? (run.reason.retryable ? "是" : "否") : "不适用"}</dd>
            </div>
            <div>
              <dt>恢复方式</dt>
              <dd>{run.reason ? recoveryLabel(run.reason.recovery) : "无需恢复"}</dd>
            </div>
          </dl>
          {run.reason || run.error_code ? (
            <p role="status">
              {run.reason ? run.reason.message : "发布未完成。"}
              {run.reason ? `；影响：${reasonImpactLabel(run.reason.impact)}` : ""}
              {run.reason?.next_action ? `；恢复动作：${run.reason.next_action}` : ""}
              {run.error_code ? `；错误类型：${runErrorLabel(run.error_code)}` : ""}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

function PublicationHistory({
  history,
  requestedSession,
}: {
  history: MacroThesisDetailReadData["history"];
  requestedSession: string;
}) {
  return (
    <section className="macro-research-citations">
      <header>
        <h3>不可变主线历史</h3>
        <span>请求交易日 {requestedSession}</span>
      </header>
      {history.length ? (
        <ol>
          {history.map((item) => (
            <li key={item.publication_id}>
              <span>{item.session_date}</span>
              <div>
                <strong>{item.title}</strong>
                <small>
                  {stanceLabel(item.stance)} · {confidenceLabel(item.confidence)} ·{" "}
                  {horizonLabel(item.horizon)} · 发布 {formatInstant(item.published_at_ms)}
                </small>
              </div>
              <Link
                aria-current={item.session_date === requestedSession ? "page" : undefined}
                to={`/macro/research?session_date=${item.session_date}`}
              >
                读取档案
              </Link>
            </li>
          ))}
        </ol>
      ) : (
        <p>尚无通过 publication gate 的不可变档案。</p>
      )}
    </section>
  );
}

function reasonText(data: MacroThesisDetailReadData, fallback: string): string {
  const reason = data.reason ?? data.run?.reason;
  if (!reason) return fallback;
  return `${reason.message}；影响：${reasonImpactLabel(reason.impact)}${
    reason.next_action ? `；恢复动作：${reason.next_action}` : ""
  }`;
}

function reasonImpactLabel(value: "none" | "limited" | "blocked"): string {
  return {
    blocked: "阻断当前判断",
    limited: "限制判断范围",
    none: "不影响已发布判断",
  }[value];
}

function thesisStateTone(value: MacroThesisDetailReadData["state"]) {
  if (value === "current") return "positive" as const;
  if (value === "historical" || value === "generating") return "caution" as const;
  return "negative" as const;
}

function runStatusTone(value: string) {
  if (value === "published") return "positive" as const;
  if (value === "running" || value === "pending" || value === "retryable") {
    return "caution" as const;
  }
  return "negative" as const;
}

function runReaderStatus(value: string): string {
  if (value === "not_published") return "审阅未通过，未发布";
  return runStatusLabel(value);
}

function recoveryLabel(value: MacroReason["recovery"]): string {
  return {
    automatic: "系统自动恢复",
    next_session: "下一交易日重新生成",
    none: "无需恢复",
    operator_action: "需要操作员处理",
  }[value];
}
