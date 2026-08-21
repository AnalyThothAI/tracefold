import { ApiError } from "@lib/api/client";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card, CardNote } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { type ReactNode, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  NEWS_REVIEW_DEFAULT_HOURS,
  NEWS_REVIEW_HOURS,
  type NewsBlindPairwiseSubmission,
  type NewsEventRubricSubmission,
  type NewsExternalMissSubmission,
  type NewsReview,
  type NewsReviewTask,
  useNewsReviewEvidenceWithToken,
  useNewsReviewWithToken,
  useSubmitExternalMiss,
  useSubmitNewsReview,
} from "../../api/newsQueries";
import { formatCount } from "../../model/newsLabels";
import { NewsEmptyNote, NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";

import "./newsReview.css";

type ReviewView = "queue" | "coverage" | "proposals" | "market";
type PairPreference = "A" | "B" | "tie" | "both_bad" | "uncertain";
type DimensionResult = "pass" | "fail" | "uncertain" | "not_applicable";
type FirstBadOwner = NonNullable<NewsEventRubricSubmission["first_bad_owner"]>;
type PairCriticalError = NonNullable<NewsBlindPairwiseSubmission["critical_errors"]>[number];

const PAIR_CRITICAL_ERROR_LABELS: Array<[PairCriticalError, string]> = [
  ["A:unsupported_fact", "A 有无证据事实"],
  ["A:wrong_entity", "A 绑错实体/资产"],
  ["A:wrong_direction", "A 方向错误"],
  ["A:missed_key_fact", "A 漏掉关键事实"],
  ["A:near_duplicate", "A 是严重重复"],
  ["A:injection_obedience", "A 服从注入指令"],
  ["B:unsupported_fact", "B 有无证据事实"],
  ["B:wrong_entity", "B 绑错实体/资产"],
  ["B:wrong_direction", "B 方向错误"],
  ["B:missed_key_fact", "B 漏掉关键事实"],
  ["B:near_duplicate", "B 是严重重复"],
  ["B:injection_obedience", "B 服从注入指令"],
];

const DIMENSION_LABELS: Record<string, string> = {
  asset_grounding: "标的对应正确",
  direction: "方向正确",
  factual_fidelity: "事实忠实",
  headline_fidelity: "标题忠实",
  magnitude: "重要程度正确",
  timeliness: "送达及时",
  why_support: "Why 有证据",
  why_value: "Why 有用",
};

const VIEW_LABELS: Array<[ReviewView, string]> = [
  ["queue", "复盘队列"],
  ["coverage", "证据覆盖"],
  ["proposals", "候选方案"],
  ["market", "市场旁证"],
];

export function NewsReviewPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const view = parseView(searchParams.get("view"));
  const mode = searchParams.get("mode") === "pairwise" ? "pairwise" : "event";
  const hours = parseHours(searchParams.get("hours"));
  const event = searchParams.get("event") || undefined;
  const task = searchParams.get("task") || undefined;
  const query = useNewsReviewWithToken(token, { event, hours, mode, task, view });
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    next.set(key, value);
    setSearchParams(next, { replace: true });
  };
  const setView = (nextView: ReviewView) => {
    const next = new URLSearchParams(searchParams);
    next.set("view", nextView);
    if (nextView === "market" && hours > 168) next.set("hours", "168");
    setSearchParams(next, { replace: true });
  };
  const setMode = (nextMode: "event" | "pairwise") => {
    const next = new URLSearchParams(searchParams);
    next.set("mode", nextMode);
    next.delete("task");
    next.delete("cursor");
    setSearchParams(next, { replace: true });
  };
  const setTask = (nextTask: NewsReviewTask | null) => {
    const next = new URLSearchParams(searchParams);
    if (nextTask) next.set("task", nextTask.task_id);
    else next.delete("task");
    setSearchParams(next, { replace: true });
  };
  const hourOptions =
    view === "market" ? NEWS_REVIEW_HOURS.filter((option) => option <= 168) : NEWS_REVIEW_HOURS;

  return (
    <NewsPageShell archetype="scan" className="news-review-shell" label="学习复盘">
      <NewsPageHeader
        subtitle="先对齐真实事件、Agent 判断、读者是否收到和人工复核，再允许候选 Prompt 或策略进入影子验证。价格只作旁证。"
        title="学习复盘"
      >
        <label className="news-review-window">
          <span className="sr-only">复盘窗口</span>
          <select onChange={(event) => setParam("hours", event.target.value)} value={String(hours)}>
            {hourOptions.map((option) => (
              <option key={option} value={option}>
                {windowLabel(option)}
              </option>
            ))}
          </select>
        </label>
      </NewsPageHeader>

      <nav aria-label="复盘视图" className="news-review-tabs">
        {VIEW_LABELS.map(([key, label]) => (
          <button
            aria-current={view === key ? "page" : undefined}
            key={key}
            onClick={() => setView(key)}
          >
            {label}
          </button>
        ))}
      </nav>

      {query.isLoading && !query.data ? (
        <PageState.Loading label="正在读取学习证据" layout="panel" rows={4} />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {query.data ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          {view === "queue" ? (
            <QueueView
              mode={mode}
              onMode={setMode}
              onSelect={setTask}
              review={query.data}
              selectedTaskId={task}
              token={token}
            />
          ) : null}
          {view === "coverage" ? <CoverageView review={query.data} /> : null}
          {view === "proposals" ? <ProposalView review={query.data} /> : null}
          {view === "market" ? <MarketView review={query.data} /> : null}
        </PageState.Stale>
      ) : null}
    </NewsPageShell>
  );
}

function QueueView({
  mode,
  onMode,
  onSelect,
  review,
  selectedTaskId,
  token,
}: {
  mode: "event" | "pairwise";
  onMode: (mode: "event" | "pairwise") => void;
  onSelect: (task: NewsReviewTask | null) => void;
  review: NewsReview;
  selectedTaskId?: string;
  token: string;
}) {
  const selected = review.tasks?.find((task) => task.task_id === selectedTaskId) ?? null;
  return (
    <div className="news-review-body">
      <div aria-label="复盘模式" className="news-review-mode" role="group">
        <ActionButton
          onClick={() => onMode("event")}
          variant={mode === "event" ? "primary" : "secondary"}
        >
          事件 Rubric
        </ActionButton>
        <ActionButton
          onClick={() => onMode("pairwise")}
          variant={mode === "pairwise" ? "primary" : "secondary"}
        >
          匿名 A/B
        </ActionButton>
      </div>
      <div className="news-review-workspace">
        <Card
          flush
          hint="只显示尚未接受的任务"
          title={mode === "event" ? "待复盘事件" : "候选输出盲测"}
        >
          {review.tasks?.length ? (
            <div className="news-review-task-list">
              {review.tasks.map((task) => (
                <button
                  aria-pressed={selected?.task_id === task.task_id}
                  key={task.task_id}
                  onClick={() => onSelect(task)}
                >
                  <span>
                    {task.headline || (task.mode === "pairwise" ? "匿名输出 A / B" : task.task_id)}
                  </span>
                  <small>
                    {task.selection.stratum_zh || "未识别复盘分层"}
                    {task.final_decision ? ` · ${task.final_decision_zh || "未识别送达决定"}` : ""}
                  </small>
                  {task.reader_receipt ? (
                    <b>{task.reader_receipt.truth_zh || "送达状态未知"}</b>
                  ) : null}
                </button>
              ))}
            </div>
          ) : (
            <NewsEmptyNote>{review.message_zh || "当前没有待复盘任务。"}</NewsEmptyNote>
          )}
        </Card>
        <Card title={selected ? "证据与判断" : "选择一条任务"}>
          {selected ? (
            <EvidenceReview
              key={selected.task_id}
              onAccepted={onSelect}
              task={selected}
              token={token}
            />
          ) : (
            <NewsEmptyNote>左侧选择任务后，这里显示当时证据和提交表单。</NewsEmptyNote>
          )}
        </Card>
      </div>
      {mode === "event" ? <ExternalMissForm token={token} /> : null}
    </div>
  );
}

function EvidenceReview({
  onAccepted,
  task,
  token,
}: {
  onAccepted: (next: NewsReviewTask | null) => void;
  task: NewsReviewTask;
  token: string;
}) {
  const evidenceQuery = useNewsReviewEvidenceWithToken(token, task);
  if (evidenceQuery.isLoading)
    return <PageState.Loading label="正在读取冻结证据" layout="inline" rows={3} />;
  if (evidenceQuery.isError)
    return (
      <PageState.Error error={evidenceQuery.error} onRetry={() => void evidenceQuery.refetch()} />
    );
  if (!evidenceQuery.data) return null;
  if (task.mode === "pairwise")
    return (
      <PairwiseForm
        evidence={evidenceQuery.data}
        onAccepted={onAccepted}
        task={task}
        token={token}
      />
    );
  return (
    <EventRubricForm
      evidence={evidenceQuery.data}
      onAccepted={onAccepted}
      task={task}
      token={token}
    />
  );
}

function EventRubricForm({
  evidence,
  onAccepted,
  task,
  token,
}: {
  evidence: NonNullable<ReturnType<typeof useNewsReviewEvidenceWithToken>["data"]>;
  onAccepted: (next: NewsReviewTask | null) => void;
  task: NewsReviewTask;
  token: string;
}) {
  const submit = useSubmitNewsReview(token);
  const rubric = asRecord(evidence.rubric);
  const rubricDimensions = Array.isArray(rubric.dimensions)
    ? rubric.dimensions.map(String)
    : ["factual_fidelity", "headline_fidelity", "why_support", "why_value", "timeliness"];
  const [shouldPush, setShouldPush] =
    useState<NewsEventRubricSubmission["should_push"]>("uncertain");
  const [dimensions, setDimensions] = useState<Record<string, DimensionResult>>(() =>
    Object.fromEntries(rubricDimensions.map((dimension) => [dimension, "uncertain"])),
  );
  const [novelty, setNovelty] =
    useState<NewsEventRubricSubmission["novelty"]["judgment"]>("uncertain");
  const [duplicateOf, setDuplicateOf] = useState("");
  const [note, setNote] = useState("");
  const [firstBadOwner, setFirstBadOwner] = useState<FirstBadOwner | "">("");
  const source = evidence.evidence as Record<string, unknown> | undefined;
  const focus = (source?.focus_fact ?? {}) as Record<string, unknown>;
  const agent = evidence.agent ?? {};
  const verdict = agent.verdict as Record<string, unknown> | undefined;
  const flags = Array.isArray(agent.verifier_flags) ? agent.verifier_flags.map(asRecord) : [];
  return (
    <form
      className="news-review-form"
      onSubmit={(event) => {
        event.preventDefault();
        const hasFailure = Object.values(dimensions).includes("fail");
        const body: NewsEventRubricSubmission = {
          kind: "event_rubric",
          should_push: shouldPush,
          dimensions,
          novelty: {
            duplicate_of: novelty === "restatement" ? duplicateOf : "",
            judgment: novelty,
          },
          evidence_refs: hasFailure ? ["source:focus_fact", "agent:output"] : [],
          expected_correction: hasFailure ? note || "按冻结证据修正判断或文案。" : "",
          first_bad_owner: firstBadOwner || null,
          note,
        };
        submit.mutate(
          {
            task,
            body,
          },
          { onSuccess: (receipt) => onAccepted(receipt.next_task ?? null) },
        );
      }}
    >
      <EvidenceBlock title="模型当时看到的事实">
        <p>{String(focus.text || task.headline || "—")}</p>
        {focus.context ? <small>{String(focus.context)}</small> : null}
      </EvidenceBlock>
      <EvidenceBlock title="Agent 输出">
        <p>{String(verdict?.headline_zh || task.agent_headline || "—")}</p>
        <small>{String(verdict?.why_zh || task.agent_why || "—")}</small>
      </EvidenceBlock>
      {flags.length ? (
        <section aria-label="确定性检查" className="news-review-verifier">
          {flags.map((flag) => (
            <p data-severity={String(flag.severity || "warning")} key={String(flag.code)}>
              <b>{String(flag.code)}</b>
              {String(flag.message_zh || "")}
            </p>
          ))}
        </section>
      ) : null}
      <div className="news-review-fields">
        <SelectField
          label="应该推送"
          onChange={(value) => setShouldPush(value as NewsEventRubricSubmission["should_push"])}
          value={shouldPush}
          values={["must_push", "should_push", "should_hold", "must_hold", "uncertain"]}
        />
        {rubricDimensions.map((dimension) => (
          <SelectField
            key={dimension}
            label={DIMENSION_LABELS[dimension] || dimension}
            onChange={(value) =>
              setDimensions((current) => ({ ...current, [dimension]: value as DimensionResult }))
            }
            value={dimensions[dimension] || "uncertain"}
            values={["pass", "fail", "uncertain", "not_applicable"]}
          />
        ))}
        <SelectField
          label="新颖度"
          onChange={(value) =>
            setNovelty(value as NewsEventRubricSubmission["novelty"]["judgment"])
          }
          value={novelty}
          values={["new_fact", "progression", "restatement", "uncertain"]}
        />
        <SelectField
          label="第一责任环节"
          onChange={(value) => setFirstBadOwner(value as FirstBadOwner | "")}
          value={firstBadOwner}
          values={[
            "",
            "receiver",
            "deduper",
            "event_evidence",
            "gate",
            "retrieval",
            "storyline",
            "triage_prompt",
            "model",
            "policy",
            "delivery",
            "taxonomy",
            "unknown",
          ]}
        />
      </div>
      {novelty === "restatement" ? (
        <label>
          重复的是哪个 Event
          <input
            onChange={(event) => setDuplicateOf(event.target.value)}
            placeholder="event_id"
            required
            value={duplicateOf}
          />
        </label>
      ) : null}
      <label>
        说明或期望修正
        <textarea onChange={(event) => setNote(event.target.value)} rows={3} value={note} />
      </label>
      <ActionButton disabled={submit.isPending} type="submit" variant="primary">
        {submit.isPending ? "提交中…" : "提交并下一条"}
      </ActionButton>
      {submit.isSuccess ? <p className="news-review-success">已写入不可变复盘记录。</p> : null}
      {submit.isError ? (
        <p aria-live="assertive" className="news-review-error" role="alert">
          {reviewMutationError(submit.error)}
        </p>
      ) : null}
      <details className="news-review-technical">
        <summary>冻结技术证据</summary>
        <pre>
          {JSON.stringify(
            {
              trace: agent.trace,
              reader_receipt: evidence.reader_receipt,
              versions: evidence.versions,
              market_reactions:
                evidence.disclosure?.market_revealed === true
                  ? evidence.market_reactions
                  : "提交复盘后显示；不参与 should-push 判断",
            },
            null,
            2,
          )}
        </pre>
      </details>
    </form>
  );
}

function PairwiseForm({
  evidence,
  onAccepted,
  task,
  token,
}: {
  evidence: NonNullable<ReturnType<typeof useNewsReviewEvidenceWithToken>["data"]>;
  onAccepted: (next: NewsReviewTask | null) => void;
  task: NewsReviewTask;
  token: string;
}) {
  const submit = useSubmitNewsReview(token);
  const [preference, setPreference] = useState<PairPreference>("uncertain");
  const [criticalErrors, setCriticalErrors] = useState<PairCriticalError[]>([]);
  const reveal = asRecord(evidence.reveal);
  const isDevelopment = evidence.disclosure?.dataset_role === "development";
  return (
    <form
      className="news-review-form"
      onSubmit={(event) => {
        event.preventDefault();
        const body: NewsBlindPairwiseSubmission = {
          kind: "blind_pairwise",
          preference,
          critical_errors: criticalErrors,
          evidence_refs: ["output:A", "output:B"],
          note: "",
        };
        submit.mutate(
          {
            task,
            body,
          },
          {
            onSuccess: (receipt) => onAccepted(isDevelopment ? task : (receipt.next_task ?? null)),
          },
        );
      }}
    >
      <p className="news-review-blind-note">
        {reveal.arm_identity_revealed
          ? "这条开发样本已完成判断，下面显示稳定版、候选版和精确改动。"
          : "系统不会显示哪一边是稳定版或候选版，也不会显示事后价格。"}
      </p>
      <div className="news-review-pair">
        <BlindOutput label="A" output={evidence.output_A ?? {}} />
        <BlindOutput label="B" output={evidence.output_B ?? {}} />
      </div>
      <SelectField
        label="更好的输出"
        onChange={(value) => setPreference(value as PairPreference)}
        value={preference}
        values={["A", "B", "tie", "both_bad", "uncertain"]}
      />
      <fieldset className="news-review-critical-errors">
        <legend>严重错误（可多选，直接进入发布安全门）</legend>
        {PAIR_CRITICAL_ERROR_LABELS.map(([value, label]) => (
          <label key={value}>
            <input
              checked={criticalErrors.includes(value)}
              onChange={(event) =>
                setCriticalErrors((current) =>
                  event.target.checked
                    ? [...current, value]
                    : current.filter((item) => item !== value),
                )
              }
              type="checkbox"
            />
            {label}
          </label>
        ))}
      </fieldset>
      <ActionButton disabled={submit.isPending} type="submit" variant="primary">
        提交匿名比较
      </ActionButton>
      {reveal.arm_identity_revealed ? (
        <section className="news-review-reveal" aria-label="盲评揭盲结果">
          <h3>已揭盲</h3>
          <p>
            稳定版是 {String(reveal.stable_side)}，候选版是 {String(reveal.candidate_side)}
            ；你的选择对应
            {String(reveal.preferred_arm || "uncertain")}。
          </p>
          <p>{String(reveal.hypothesis || "未记录候选假设")}</p>
          <pre>{JSON.stringify(reveal.exact_diff ?? {}, null, 2)}</pre>
        </section>
      ) : null}
      {submit.isError ? (
        <p aria-live="assertive" className="news-review-error" role="alert">
          {reviewMutationError(submit.error)}
        </p>
      ) : null}
    </form>
  );
}

function ExternalMissForm({ token }: { token: string }) {
  const submit = useSubmitExternalMiss(token);
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [body, setBody] = useState("");
  const [occurredAt, setOccurredAt] = useState(() => localDateTimeValue(Date.now()));
  return (
    <Card hint="记录系统根本没形成 Event 的重点事实" title="外部漏召回">
      <form
        className="news-review-external"
        onSubmit={(event) => {
          event.preventDefault();
          const submission: NewsExternalMissSubmission = {
            kind: "external_miss",
            source_url: url,
            title,
            body,
            occurred_at_ms: new Date(occurredAt).getTime(),
            rubric: {
              kind: "event_rubric",
              should_push: "must_push",
              dimensions: {
                factual_fidelity: "uncertain",
                headline_fidelity: "not_applicable",
                timeliness: "fail",
                why_support: "not_applicable",
                why_value: "not_applicable",
              },
              novelty: { duplicate_of: "", judgment: "new_fact" },
              evidence_refs: ["external:source_url"],
              expected_correction: "让上游 Receiver/Deduper 能形成可判断的 Event。",
              note: "系统未形成 Event",
            },
          };
          submit.mutate(submission);
        }}
      >
        <label>
          标题
          <input onChange={(event) => setTitle(event.target.value)} required value={title} />
        </label>
        <label>
          来源 URL
          <input onChange={(event) => setUrl(event.target.value)} required type="url" value={url} />
        </label>
        <label>
          事实发生时间
          <input
            max={localDateTimeValue(Date.now())}
            onChange={(event) => setOccurredAt(event.target.value)}
            required
            type="datetime-local"
            value={occurredAt}
          />
        </label>
        <label>
          正文摘录
          <textarea onChange={(event) => setBody(event.target.value)} rows={3} value={body} />
        </label>
        <ActionButton disabled={submit.isPending} type="submit">
          记录漏召回
        </ActionButton>
        {submit.isSuccess ? (
          <p className="news-review-success">漏召回证据和初始复盘已一起封存。</p>
        ) : null}
        {submit.isError ? (
          <p aria-live="assertive" className="news-review-error" role="alert">
            {reviewMutationError(submit.error)}
          </p>
        ) : null}
      </form>
    </Card>
  );
}

function CoverageView({ review }: { review: NewsReview }) {
  const funnel = review.funnel;
  if (!funnel) return <NewsEmptyNote>{review.message_zh || "还没有覆盖证据。"}</NewsEmptyNote>;
  return (
    <div className="news-review-body">
      <Card hint="百分比只表示人工证据覆盖，不是模型准确率" title="复盘证据漏斗">
        <MetricRow columns={5} label="学习证据覆盖">
          <Metric caption="真实收到" eyebrow="RECEIVED" value={formatCount(funnel.received)} />
          <Metric caption="可回放" eyebrow="REPLAYABLE" value={formatCount(funnel.replayable)} />
          <Metric caption="已复盘" eyebrow="REVIEWED" value={formatCount(funnel.reviewed)} />
          <Metric caption="已接受" eyebrow="ACCEPTED" value={formatCount(funnel.accepted)} />
          <Metric
            caption="外部漏召回"
            eyebrow="EXTERNAL"
            value={formatCount(funnel.external_misses)}
          />
        </MetricRow>
      </Card>
      <HoldoutCoverage holdout={review.holdout} />
      <CoverageBuckets rows={review.strata ?? []} title="抽样层" />
      <CoverageBuckets rows={review.cohorts ?? []} title="版本 Cohort" />
    </div>
  );
}

function HoldoutCoverage({ holdout }: { holdout: NewsReview["holdout"] }) {
  if (!holdout) return null;
  const interval = holdout.coverage_interval_95;
  return (
    <Card hint="一条事实即使被多个来源重复报道，也只算一个独立样本" title="隐藏时间留出集">
      <p className="news-review-disclaimer">
        {holdout.status === "ready"
          ? `已接受 ${formatCount(holdout.accepted_cluster_n)} / ${formatCount(holdout.cluster_n)} 个独立事实簇。`
          : `证据不足：目前只有 ${formatCount(holdout.accepted_cluster_n)} 个已接受独立事实簇。`}
      </p>
      <CardNote>
        覆盖率 {holdout.coverage_pct == null ? "—" : `${holdout.coverage_pct}%`}
        {interval ? `（95% 区间 ${interval.lower_pct}%–${interval.upper_pct}%）` : ""}
      </CardNote>
    </Card>
  );
}

function CoverageBuckets({ rows, title }: { rows: NewsReview["strata"]; title: string }) {
  return (
    <Card flush title={title}>
      {rows?.length ? (
        <div className="news-review-coverage-list">
          {rows.map((row, index) => (
            <div key={`${row.stratum || row.cohort}-${index}`}>
              <span title={row.cohort || undefined}>
                {row.stratum
                  ? row.stratum_zh || "未识别复盘分层"
                  : `${row.legacy_cohort || "Agent"} · ${row.cohort?.slice(0, 8) || "未知"}`}
              </span>
              <b>
                {formatCount(row.accepted)} / {formatCount(row.events)}
              </b>
              <small>{row.accepted_pct == null ? "证据不足" : `${row.accepted_pct}%`}</small>
            </div>
          ))}
        </div>
      ) : (
        <NewsEmptyNote>暂无样本。</NewsEmptyNote>
      )}
    </Card>
  );
}

function ProposalView({ review }: { review: NewsReview }) {
  return (
    <Card title="候选方案与发布证据">
      {review.proposals?.length ? (
        <div className="news-review-proposals">
          {review.proposals.map((raw, index) => {
            const proposal = asRecord(raw);
            const timeline = Array.isArray(proposal.timeline)
              ? proposal.timeline.map(asRecord)
              : [];
            return (
              <article key={String(proposal.candidate_sha || index)}>
                <header>
                  <span>{String(proposal.status_zh || "证据状态未知")}</span>
                  <code>{shortSha(proposal.candidate_sha)}</code>
                </header>
                <h3>{String(proposal.hypothesis || "未填写假设")}</h3>
                <p>
                  单变量：{String(proposal.target_zh || "未知变更")} · 目标维度：
                  {Array.isArray(proposal.target_dimensions_zh)
                    ? proposal.target_dimensions_zh.join("、")
                    : "—"}
                </p>
                <dl>
                  <div>
                    <dt>失败簇</dt>
                    <dd>
                      {Array.isArray(proposal.failure_cluster_ids)
                        ? proposal.failure_cluster_ids.length
                        : 0}
                    </dd>
                  </div>
                  <div>
                    <dt>Development</dt>
                    <dd>{shortSha(proposal.development_dataset_sha)}</dd>
                  </div>
                  <div>
                    <dt>Parent stable</dt>
                    <dd>{shortSha(proposal.parent_stable_sha)}</dd>
                  </div>
                </dl>
                <ol aria-label="发布证据链">
                  {timeline.length ? (
                    timeline.map((step, stepIndex) => (
                      <li key={`${String(step.report_sha)}-${stepIndex}`}>
                        <b>{String(step.stage_zh || "未知阶段")}</b>
                        <span data-outcome={String(step.outcome || "unknown")}>
                          {String(step.outcome_zh || "证据状态未知")}
                        </span>
                        <small>
                          {Array.isArray(step.blockers_zh) && step.blockers_zh.length
                            ? `阻塞：${step.blockers_zh.join("、")}`
                            : Array.isArray(step.failures_zh) && step.failures_zh.length
                              ? `失败：${step.failures_zh.join("、")}`
                              : "证据已封存"}
                        </small>
                      </li>
                    ))
                  ) : (
                    <li>
                      <b>PROPOSED</b>
                      <span data-outcome="unknown">WAITING</span>
                      <small>尚未产生评估证据</small>
                    </li>
                  )}
                </ol>
                {proposal.exact_diff ? (
                  <details className="news-review-technical">
                    <summary>查看候选的精确单变量差异</summary>
                    <pre>{JSON.stringify(proposal.exact_diff, null, 2)}</pre>
                  </details>
                ) : proposal.diff_withheld_reason ? (
                  <CardNote>隐藏留出集仍在盲评，暂不显示候选差异。</CardNote>
                ) : null}
                <CardNote>
                  这里没有“设为稳定版”按钮；发布和回滚只允许 CLI + 正常 Git/部署流程。
                </CardNote>
              </article>
            );
          })}
        </div>
      ) : (
        <NewsEmptyNote>
          {review.message_zh || "还没有封存的候选评估。先完成分层复盘和 development dataset。"}
        </NewsEmptyNote>
      )}
    </Card>
  );
}

function MarketView({ review }: { review: NewsReview }) {
  const reaction = review.reaction;
  const meta = asRecord(reaction?.meta);
  const misses = reaction?.potential_misses ?? [];
  return (
    <div className="news-review-body">
      <Card hint="不进入奖励、不自动写复盘" title={review.title_zh || "事后市场观察"}>
        <p className="news-review-disclaimer">{review.disclaimer_zh}</p>
        <CardNote>
          新闻后的 1H / 4H
          涨跌可能来自大盘、行业、其他新闻或随机波动。这里只帮助人找案例，不能证明因果。
        </CardNote>
      </Card>
      <Card
        flush
        hint={`只看同版本 Agent：${String(meta.cohort || "证据不足")}`}
        title="价格证据覆盖"
      >
        {reaction?.coverage?.length ? (
          <div className="news-review-market-coverage">
            {reaction.coverage.map((row) => (
              <div key={row.horizon}>
                <b>{row.horizon_zh || row.horizon}</b>
                <span>
                  {formatCount(row.priced_n)} / {formatCount(row.eligible_n)} 个成熟事件有价格
                </span>
                <small>{row.coverage_pct == null ? "证据不足" : `覆盖 ${row.coverage_pct}%`}</small>
              </div>
            ))}
          </div>
        ) : (
          <NewsEmptyNote>{review.message_zh || "当前 cohort 没有成熟的价格观察。"}</NewsEmptyNote>
        )}
      </Card>
      <Card
        flush
        hint="线索最多回看 7 天；先按 4 小时内相似文本聚成事实簇；排序只表示后续波动较大"
        title="值得人工查看的事实线索"
      >
        {misses.length ? (
          <div className="news-review-market-cases">
            {misses.map((raw) => {
              const item = asRecord(raw);
              const clusterN = Number(item.fact_cluster_n || 1);
              return (
                <article key={String(item.fact_cluster_key || item.event_id)}>
                  <div>
                    <span>{String(item.decision_zh || "未送达")}</span>
                    {clusterN > 1 ? <b>{clusterN} 个 Event / 1 个事实</b> : <b>1 个事实</b>}
                  </div>
                  <h3>{String(item.headline_zh || item.leader_title || "未命名事实")}</h3>
                  <p>
                    1H {formatBps(item.return_1h_bps)} · 4H {formatBps(item.return_4h_bps)} · 规则
                    {String(item.override_rule_zh || item.throttled_by_zh || "未识别规则")}
                  </p>
                  <a href={`/news/events/${String(item.event_id)}`}>查看事件证据</a>
                </article>
              );
            })}
          </div>
        ) : (
          <NewsEmptyNote>当前没有可用的 held + mature price 事实线索。</NewsEmptyNote>
        )}
      </Card>
    </div>
  );
}

function EvidenceBlock({ children, title }: { children: ReactNode; title: string }) {
  return (
    <section className="news-review-evidence">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function BlindOutput({ label, output }: { label: string; output: Record<string, unknown> }) {
  return (
    <section>
      <b>{label}</b>
      <h3>{String(output.headline_zh || "—")}</h3>
      <p>{String(output.why_zh || "—")}</p>
      <small>
        {String(output.final_decision_zh || "未识别送达决定")} · 重要程度{" "}
        {String(output.magnitude ?? "—")}
      </small>
    </section>
  );
}

function SelectField({
  label,
  onChange,
  value,
  values,
}: {
  label: string;
  onChange: (value: string) => void;
  value: string;
  values: string[];
}) {
  return (
    <label>
      {label}
      <select onChange={(event) => onChange(event.target.value)} value={value}>
        {values.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function shortSha(value: unknown) {
  const sha = String(value || "");
  return sha ? `${sha.slice(0, 10)}…` : "—";
}

function formatBps(value: unknown) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  const prefix = parsed > 0 ? "+" : "";
  return `${prefix}${parsed} bps`;
}

function parseView(value: string | null): ReviewView {
  return VIEW_LABELS.some(([key]) => key === value) ? (value as ReviewView) : "queue";
}

function parseHours(value: string | null) {
  const parsed = Number(value);
  return NEWS_REVIEW_HOURS.includes(parsed) ? parsed : NEWS_REVIEW_DEFAULT_HOURS;
}

function windowLabel(hours: number) {
  return hours === 24 ? "24 小时" : hours === 72 ? "3 天" : hours === 168 ? "7 天" : "30 天";
}

function localDateTimeValue(stamp: number) {
  const date = new Date(stamp);
  const local = new Date(stamp - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function reviewMutationError(error: unknown) {
  if (!(error instanceof ApiError)) return "提交失败，请重试。";
  if (error.message.includes("news_review_task_version_conflict")) {
    return "这条任务的证据已经变化，请刷新后重新判断。";
  }
  if (error.message.includes("news_review_idempotency_conflict")) {
    return "这次提交编号已被另一份内容使用，请刷新后再提交。";
  }
  if (
    error.message.includes("review_write_unavailable") ||
    error.message.includes("review_write_busy")
  ) {
    return "复盘写入暂时不可用；新闻读取和线上推送不受影响，请稍后重试。";
  }
  return error.message || "提交失败，请重试。";
}
