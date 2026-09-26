import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { FactGrid } from "@shared/ui/FactGrid";
import { ExternalLink } from "lucide-react";

import type {
  NewsClaim,
  NewsClaimChange,
  NewsEventUpdate,
  NewsProcessing,
  NewsUpdateEvidence,
  NewsUpdateSource,
} from "../../api/newsQueries";
import { absoluteTime, optionalTime, validExternalUrl } from "../../model/newsLabels";

import "./newsEventUpdate.css";

/**
 * The adopted EventUpdate (#706), in the order a reader asks about it: what is new, what exactly is claimed,
 * who says so and who disagrees, and what is only inferred or still missing. Every business word is the
 * server's `*_zh`; the headings and field labels here are the page's own. A claim keeps its own mode, phase,
 * time and conditions -- nothing is merged into one Event-wide verdict or market direction.
 */
export function NewsEventUpdateSections({ update }: { update: NewsEventUpdate }) {
  return (
    <>
      <Card
        aria-label="新增了什么"
        hint={`内容版本 ${update.content_revision.slice(0, 8)}`}
        title="新增了什么"
      >
        <ChangeList changes={update.changes ?? []} />
      </Card>
      <Card
        aria-label="命题"
        hint={`${update.claims.length} 条，逐条保留方式、阶段、时间与条件`}
        title="命题"
      >
        <ol className="news-update-claims">
          {update.claims.map((claim) => (
            <ClaimItem claim={claim} key={claim.ref} />
          ))}
        </ol>
      </Card>
      <Card aria-label="来源与分歧" hint={sourcesHint(update)} title="来源与分歧">
        <SourceList sources={update.sources ?? []} />
      </Card>
      <Card
        aria-label="推断与缺口"
        hint="以下是解释性推断与待补信息，不是已确认事实"
        title="推断与缺口"
      >
        <InferenceList update={update} />
      </Card>
    </>
  );
}

function sourcesHint(update: NewsEventUpdate): string {
  const disputed = update.disputed_claim_refs?.length ?? 0;
  const count = `${update.sources?.length ?? 0} 个来源`;
  return disputed ? `${count} · ${disputed} 条命题来源分歧` : count;
}

function ChangeList({ changes }: { changes: NewsClaimChange[] }) {
  if (!changes.length) return <EmptyNote>这一版没有相对此前命题的变化。</EmptyNote>;
  return (
    <ol className="news-update-changes">
      {changes.map((change, index) => (
        <li className="news-update-change" key={`${change.current_ref}-${change.kind}-${index}`}>
          <span className="news-update-badge" data-kind={change.kind}>
            {change.kind_zh || change.kind}
          </span>
          <p>{change.current_statement || change.current_ref}</p>
          {change.previous_ref ? (
            <p className="news-update-previous">
              此前：
              {change.previous_statement ??
                `未能读取此前命题（${change.previous_ref.slice(0, 16)}…）`}
              {change.relation_zh ? <small> · {change.relation_zh}</small> : null}
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function quantityText(claim: NewsClaim): string {
  return (claim.quantities ?? [])
    .map(
      (quantity) =>
        `${quantity.name} ${quantity.value}${quantity.unit}${quantity.period ? `（${quantity.period}）` : ""}`,
    )
    .join("；");
}

function ClaimItem({ claim }: { claim: NewsClaim }) {
  const counts = claim.relation_counts;
  return (
    <li className="news-update-claim" data-retired={claim.retired || undefined}>
      <p className="news-update-statement">{claim.statement}</p>
      <p className="news-update-badges">
        {claim.retired ? (
          <span className="news-update-badge" data-kind="retired">
            已撤回
          </span>
        ) : null}
        {claim.disputed ? (
          <span className="news-update-badge" data-kind="disputed">
            来源分歧
          </span>
        ) : null}
        <span className="news-update-badge">{claim.mode_zh || claim.mode}</span>
        {claim.phase_zh ? <span className="news-update-badge">{claim.phase_zh}</span> : null}
        <span className="news-update-badge">{claim.content_kind_zh || claim.content_kind}</span>
        {claim.polarity === "negative" ? (
          <span className="news-update-badge">{claim.polarity_zh}</span>
        ) : null}
      </p>
      <FactGrid
        className="news-update-facts"
        facts={[
          { label: "主体", value: claim.subject },
          { label: "动作", value: claim.action },
          { label: "对象", value: claim.object ?? "" },
          { label: "说话者", value: claim.speaker ?? "" },
          { label: "生效时间", value: claim.effective_at ?? "" },
          { label: "发生时间", value: claim.occurred_at ?? "" },
          { label: "统计期", value: claim.statistical_period ?? "" },
          { label: "条件", value: (claim.conditions ?? []).join("；") },
          { label: "数值", value: quantityText(claim) },
          {
            label: "标的",
            value: (claim.assets ?? [])
              .map((asset) => `${asset.symbol}${asset.role === "primary" ? "" : "（提及）"}`)
              .join(" "),
          },
          {
            label: "来源关系",
            value: counts
              ? [
                  counts.supports ? `支持 ${counts.supports}` : "",
                  counts.reports ? `转述 ${counts.reports}` : "",
                  counts.refutes ? `反驳 ${counts.refutes}` : "",
                  counts.unresolved ? `未判定 ${counts.unresolved}` : "",
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "",
          },
          { label: "首次可用", value: absoluteTime(claim.first_available_at_ms) },
        ]}
        label="命题字段"
      />
      {claim.citations.map((citation, index) => (
        <blockquote className="news-update-quote" key={`${citation.evidence_ref}-${index}`}>
          <p>{citation.quote}</p>
          {citation.source ? (
            <footer>
              <SourceLabel source={citation.source} />
            </footer>
          ) : null}
        </blockquote>
      ))}
    </li>
  );
}

function SourceLabel({ source }: { source: NewsUpdateSource }) {
  const url = validExternalUrl(source.url);
  return (
    <span className="news-update-source-label">
      <b>{source.publisher_id}</b>
      {source.attribution ? <span>{source.attribution}</span> : null}
      <span>{source.source_authority_zh || source.source_authority}</span>
      {source.published_at_ms ? <span>{optionalTime(source.published_at_ms)}</span> : null}
      {url ? (
        <a href={url} rel="noreferrer" target="_blank">
          原文
          <ExternalLink aria-hidden />
        </a>
      ) : null}
    </span>
  );
}

function SourceList({ sources }: { sources: NewsUpdateEvidence[] }) {
  if (!sources.length) return <EmptyNote>这一版没有引用来源。</EmptyNote>;
  return (
    <ol className="news-update-sources">
      {sources.map((item) => (
        <li className="news-update-source" key={item.evidence_ref}>
          <SourceLabel source={item.source} />
          <ul className="news-update-relations">
            {(item.relations ?? []).map((relation) => (
              <li
                data-relation={relation.relation}
                key={`${relation.claim_ref}-${relation.relation}`}
              >
                <span className="news-update-badge" data-kind={relation.relation}>
                  {relation.relation_zh || relation.relation}
                </span>
                <span>{relation.claim_statement || relation.claim_ref}</span>
              </li>
            ))}
          </ul>
          <details>
            <summary>来源原文{item.text_truncated ? "（节选）" : ""}</summary>
            <p className="news-update-source-text">{item.text}</p>
          </details>
        </li>
      ))}
    </ol>
  );
}

function InferenceList({ update }: { update: NewsEventUpdate }) {
  const implications = update.implications ?? [];
  const questions = update.open_questions ?? [];
  if (!implications.length && !questions.length) {
    return <EmptyNote>这一版没有推断，也没有记录缺口。</EmptyNote>;
  }
  return (
    <div className="news-update-inference">
      {implications.map((implication, index) => (
        <section
          aria-label="推断"
          className="news-update-implication"
          key={`${implication.channel}-${index}`}
        >
          <p>
            <span className="news-update-badge" data-kind="inference">
              推断 · {implication.origin_zh || implication.origin}
            </span>
            <b>{implication.channel}</b>
          </p>
          <p>{implication.explanation}</p>
          {implication.conditions?.length ? (
            <small>条件：{implication.conditions.join("；")}</small>
          ) : null}
        </section>
      ))}
      {questions.map((question, index) => (
        <section
          aria-label="缺口"
          className="news-update-question"
          key={`${question.question}-${index}`}
        >
          <p>
            <span className="news-update-badge" data-kind="gap">
              缺口
            </span>
            {question.question}
          </p>
          {question.target_ref ? <small>可读取：{question.target_ref}</small> : null}
        </section>
      ))}
    </div>
  );
}

/**
 * What the pipeline actually did, from its durable rows: the semantic work, the notification plan with one
 * named decision per claim, and every intent with its real state and the exact text a reader received.
 */
export function NewsProcessingState({ processing }: { processing: NewsProcessing }) {
  const { semantic, notification } = processing;
  const plan = notification?.plan;
  const intents = processing.intents ?? [];
  return (
    <Card aria-label="处理状态" hint="语义处理、通知选择与实际发送" title="处理状态">
      <div className="news-update-processing">
        {processing.update_error_code ? (
          <p className="news-update-alert">
            已采用版本无法按当前合同解码：{processing.update_error_code}
          </p>
        ) : null}
        <FactGrid
          facts={[
            { label: "语义处理", value: semantic ? semantic.state_zh || semantic.state : "" },
            {
              label: "材料版本",
              value: semantic
                ? `已完成 ${semantic.done_revision ?? "—"} / 最新 ${semantic.wanted_revision}`
                : "",
            },
            { label: "尝试次数", value: semantic ? String(semantic.attempts ?? 0) : "" },
            { label: "最近结果", value: semantic?.last_outcome ?? "" },
            { label: "错误", value: semantic?.last_error_code ?? "" },
            { label: "补读", value: semantic?.extra_read_state_zh ?? "" },
            {
              label: "通知",
              value: notification ? notification.state_zh || notification.state : "",
            },
            { label: "通知决定", value: plan ? `${plan.action_zh} · ${plan.reason_zh}` : "" },
            { label: "重点", value: plan?.key ? "是" : "" },
          ]}
          label="处理状态"
        />
        {notification?.plan_error_code ? (
          <p className="news-update-alert">通知计划无法解码：{notification.plan_error_code}</p>
        ) : null}
        {plan?.claim_decisions?.length ? (
          <section aria-label="逐条通知决定">
            <h4>逐条通知决定</h4>
            <ul className="news-update-decisions">
              {plan.claim_decisions.map((row) => (
                <li data-decision={row.decision} key={row.claim_ref}>
                  <span className="news-update-badge" data-kind={row.decision}>
                    {row.decision_zh || row.decision}
                  </span>
                  <span>{row.reason_zh || row.reason}</span>
                  <small>{row.statement ?? row.claim_ref}</small>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        <section aria-label="发送记录">
          <h4>发送记录</h4>
          {intents.length ? (
            <ul className="news-update-intents">
              {intents.map((intent) => (
                <li key={intent.intent_id}>
                  <p>
                    <span className="news-update-badge" data-kind={intent.state}>
                      {intent.state_zh || intent.state}
                    </span>
                    {intent.key ? <span className="news-update-badge">重点</span> : null}
                    <b>{intent.headline_zh ?? `${intent.claim_refs?.length ?? 0} 条命题`}</b>
                    <small>
                      {optionalTime(
                        intent.settled_at_ms ?? intent.attempted_at_ms ?? intent.enqueued_at_ms,
                      )}
                    </small>
                  </p>
                  {intent.error_code ? <small>错误：{intent.error_code}</small> : null}
                  {intent.body ? (
                    <details>
                      <summary>实际发送正文</summary>
                      <pre className="news-json">{intent.body}</pre>
                    </details>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyNote>还没有发送意图。</EmptyNote>
          )}
        </section>
      </div>
    </Card>
  );
}
