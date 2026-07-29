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
  runStatusLabel,
  stanceLabel,
} from "../model/macroPresentation";
import type {
  MacroResearchReadData,
  MacroThesisArchiveDetailReadData,
  MacroThesisDetailReadData,
  MacroThesisV1,
} from "../model/macroTypes";

import type { MacroPageSessionProps } from "./MacroDecisionPage";
import { MacroRecoveryMatrix, MacroThesisReport } from "./MacroThesisReport";

import "./MacroResearchAudit.css";
import "./MacroResearchDossier.css";
import "./MacroResearchPage.css";

const SESSION_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function MacroResearchPage({
  bootstrapError,
  bootstrapLoading,
  token,
}: MacroPageSessionProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSession = searchParams.get("session_date");
  const sessionDate =
    requestedSession && SESSION_DATE.test(requestedSession) ? requestedSession : null;
  const [draftSession, setDraftSession] = useState(sessionDate ?? "");
  const query = useMacroResearchQuery({ sessionDate, token });

  useEffect(() => {
    setDraftSession(sessionDate ?? "");
  }, [sessionDate]);

  if (bootstrapLoading) {
    return <PageState.Loading label="建立研究档案会话" layout="route" rows={3} />;
  }
  if (bootstrapError) {
    return <PageState.Error error={new Error("研究档案读取会话建立失败。")} />;
  }
  if (!token) {
    return (
      <PageState.Empty
        title="研究档案会话不可用"
        hint="Bootstrap 已结束但没有返回访问令牌；不会停留在无限 Loading。"
      />
    );
  }
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isPending || !query.data) {
    return <PageState.Loading label="读取 Macro Thesis" layout="route" rows={5} />;
  }
  const data = query.data;
  return (
    <PageState.Stale updating={query.isFetching && !query.isPending}>
      <section
        aria-label="Macro Thesis 档案"
        className="macro-research-workbench"
        data-page-archetype="decision"
      >
        <header className="macro-research-header">
          <div>
            <Link className="macro-research-back" to="/macro">
              <ArrowLeft aria-hidden="true" />
              返回当前主线
            </Link>
            <span>THIN DEEPAGENT · IMMUTABLE PUBLICATION</span>
            <h1>Macro Thesis 档案</h1>
            <p>不带日期只读当前 v2；显式日期只读不可变 v1/v2 历史，不做 fallback。</p>
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

        <ResearchState data={data} stale={query.isError} />
        {data.schema_version === "macro_thesis_detail_v4" ? (
          <CurrentResearch data={data} />
        ) : (
          <ArchiveResearch data={data} />
        )}
        <PublicationHistory data={data} />
        <RunAttemptPanel data={data} />
      </section>
    </PageState.Stale>
  );
}

function CurrentResearch({ data }: { data: MacroThesisDetailReadData }) {
  if (data.thesis) {
    return (
      <MacroThesisReport
        liveDelta={data.live_delta}
        outcomeReplay={data.outcome_replay}
        recovery={data.recovery}
        thesis={data.thesis}
      />
    );
  }
  return (
    <PageState.Empty
      title={data.state === "running" ? "Thin Agent 正在生成" : "当前 session 未发布"}
      hint={
        data.reason?.message ?? "当前读取不会展示上一交易日，也不会在浏览器中启动或恢复 Agent。"
      }
    />
  );
}

function ArchiveResearch({ data }: { data: MacroThesisArchiveDetailReadData }) {
  if (!data.thesis) {
    return (
      <PageState.Empty
        title="该交易日没有不可变 publication"
        hint={data.reason?.message ?? "请选择历史列表中的已发布交易日。"}
      />
    );
  }
  if (data.thesis.schema_version === "macro_thesis_v2") {
    return (
      <MacroThesisReport
        liveDelta={null}
        outcomeReplay={null}
        recovery={data.recovery}
        thesis={data.thesis}
      />
    );
  }
  return <LegacyArchive recovery={data.recovery} thesis={data.thesis} />;
}

function LegacyArchive({
  thesis,
  recovery,
}: {
  thesis: MacroThesisV1;
  recovery: MacroThesisArchiveDetailReadData["recovery"];
}) {
  return (
    <>
      <article className="macro-thesis-report macro-thesis-report__mainline">
        <span>HISTORICAL · MACRO THESIS V1</span>
        <h2>{thesis.mainline.title}</h2>
        <p>{thesis.mainline.thesis}</p>
        <p>
          {stanceLabel(thesis.mainline.stance)} · {confidenceLabel(thesis.mainline.confidence)} ·{" "}
          {horizonLabel(thesis.mainline.horizon)}
        </p>
        <ol>
          {thesis.mainline.claims.map((claim) => (
            <li key={claim.claim_id}>
              <strong>{claim.statement}</strong>
            </li>
          ))}
        </ol>
        <small>
          这是显式历史档案；Reviewer 字段仅作为旧 publication 的不可变审计记录，不参与当前生产链。
        </small>
      </article>
      {recovery.length ? <MacroRecoveryMatrix rows={recovery} /> : null}
    </>
  );
}

function ResearchState({ data, stale }: { data: MacroResearchReadData; stale: boolean }) {
  const archive = data.schema_version === "macro_thesis_archive_detail_v2";
  const session = archive ? data.requested_session_date : data.session_date;
  return (
    <section className="macro-research-state" data-state={data.state}>
      <div>
        <strong>
          {archive
            ? data.state === "historical"
              ? "显式历史档案"
              : "历史档案缺失"
            : runStatusLabel(data.state)}
        </strong>
        <span>
          Session {session} · {stale ? "传输错误，保留同一请求缓存" : "当前读取成功"}
        </span>
        {data.reason ? <small>{data.reason.message}</small> : null}
      </div>
    </section>
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
      <label htmlFor="macro-research-session-date">显式历史日期</label>
      <div>
        <input
          id="macro-research-session-date"
          onChange={(event) => onDraftChange(event.target.value)}
          type="date"
          value={draftSession}
        />
        <Button disabled={!SESSION_DATE.test(draftSession)} size="sm" type="submit">
          读取档案
        </Button>
        <Button onClick={onLatest} size="sm" type="button" variant="outline">
          当前 v2
        </Button>
      </div>
    </form>
  );
}

function PublicationHistory({ data }: { data: MacroResearchReadData }) {
  const requested =
    data.schema_version === "macro_thesis_archive_detail_v2"
      ? data.requested_session_date
      : data.session_date;
  return (
    <section className="macro-research-citations">
      <header>
        <h3>不可变 publication 历史</h3>
        <span>当前选择 {requested}</span>
      </header>
      {data.history.length ? (
        <ol>
          {data.history.map((item) => (
            <li key={item.publication_id}>
              <span>{item.session_date}</span>
              <div>
                <strong>{item.title}</strong>
                <small>
                  {item.publication_schema_version} · {stanceLabel(item.stance)} ·{" "}
                  {confidenceLabel(item.confidence)} · {horizonLabel(item.horizon)} ·{" "}
                  {formatInstant(item.published_at_ms)}
                </small>
              </div>
              <Link
                aria-current={item.session_date === requested ? "page" : undefined}
                to={`/macro/research?session_date=${item.session_date}`}
              >
                读取档案
              </Link>
            </li>
          ))}
        </ol>
      ) : (
        <p>尚无不可变 publication。</p>
      )}
    </section>
  );
}

function RunAttemptPanel({ data }: { data: MacroResearchReadData }) {
  const run = data.run;
  return (
    <section aria-labelledby="macro-research-run-title" className="macro-research-run">
      <div>
        <span>PUBLICATION ATTEMPT</span>
        <h2 id="macro-research-run-title">运行状态与四类 gate</h2>
      </div>
      {!run ? (
        <p role="status">该 session 没有持久化运行。</p>
      ) : (
        <dl>
          <div>
            <dt>状态</dt>
            <dd>{runStatusLabel(run.status)}</dd>
          </div>
          <div>
            <dt>ResearchInput</dt>
            <dd>{run.research_input_id ?? "未冻结"}</dd>
          </div>
          <div>
            <dt>尝试</dt>
            <dd>
              {run.attempt_count}/{run.max_attempts}
            </dd>
          </div>
          <div>
            <dt>失败边界</dt>
            <dd>{run.gate_category ?? "pre-draft / 无"}</dd>
          </div>
          <div>
            <dt>最后更新</dt>
            <dd>{formatInstant(run.updated_at_ms)}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}
