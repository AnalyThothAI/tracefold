import { EmptyNote } from "@shared/ui/EmptyNote";
import { FactGrid } from "@shared/ui/FactGrid";
import { ExternalLink } from "lucide-react";

import type {
  NewsClaim,
  NewsClaimChange,
  NewsEventUpdate,
  NewsProcessing,
  NewsUpdateSource,
} from "../../api/newsQueries";
import { sourceDisplayName } from "../../model/eventReader";
import { absoluteTime, optionalTime, validExternalUrl } from "../../model/newsLabels";

import "./newsEventUpdate.css";

export type NewsDetailNavigate = (target: string) => void;

function claimTarget(index: number): string {
  return "news-claim-" + (index + 1);
}

function sourceTarget(index: number): string {
  return "source-" + String(index + 1).padStart(2, "0");
}

/** One current claim is one reading unit; its comparisons do not become extra claims. */
export function NewsClaimRecords({
  update,
  onNavigate,
}: {
  update: NewsEventUpdate;
  onNavigate: NewsDetailNavigate;
}) {
  const byClaim = new Map<string, NewsClaimChange[]>();
  for (const change of update.changes ?? []) {
    const rows = byClaim.get(change.current_ref) ?? [];
    rows.push(change);
    byClaim.set(change.current_ref, rows);
  }
  const sourcePositions = new Map(
    (update.sources ?? []).map((source, index) => [source.evidence_ref, index]),
  );
  const onlySource = update.sources?.length === 1 ? update.sources[0] : null;
  const sharedSource =
    onlySource &&
    update.claims.length > 1 &&
    update.claims.every((claim) =>
      claim.citations.some((citation) => citation.evidence_ref === onlySource.evidence_ref),
    );

  return (
    <section aria-label="新增了什么" className="news-update-content">
      <header className="news-update-section-head">
        <h2>这次发生了什么</h2>
        <span>{update.claims.length} 条命题</span>
      </header>
      <p className="news-update-section-note">已记录命题原文</p>
      <section aria-label="命题">
        {update.claims.length ? (
          <ol className="news-update-claims">
            {update.claims.map((claim, index) => (
              <ClaimItem
                changes={byClaim.get(claim.ref) ?? []}
                claim={claim}
                index={index}
                key={claim.ref}
                onNavigate={onNavigate}
                sourcePositions={sourcePositions}
              />
            ))}
          </ol>
        ) : (
          <EmptyNote>当前采用版本没有命题记录。</EmptyNote>
        )}
      </section>
      {sharedSource ? (
        <p className="news-update-shared-source">
          {update.claims.length} 条命题引用同一份材料，不代表 {update.claims.length} 个独立来源。
        </p>
      ) : null}
    </section>
  );
}

export function NewsUpdateInference({ update }: { update: NewsEventUpdate }) {
  const implications = update.implications ?? [];
  const questions = update.open_questions ?? [];
  if (!implications.length && !questions.length) return null;
  return (
    <section aria-label="推断与缺口" className="news-detail-update-inference">
      <header className="news-update-section-head">
        <h3>推断与缺口</h3>
      </header>
      <p className="news-update-section-note">解释性推断与待补信息，不是已确认事实</p>
      <div className="news-update-inference">
        {implications.map((implication, index) => (
          <section
            aria-label="推断"
            className="news-update-implication"
            key={implication.channel + "-" + index}
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
            key={question.question + "-" + index}
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
    </section>
  );
}

function PriorComparisons({ changes }: { changes: NewsClaimChange[] }) {
  return (
    <div className="news-update-priors">
      {changes.map((change, index) => (
        <p
          className="news-update-previous"
          key={
            change.previous_content_ref +
            "-" +
            change.previous_ref +
            "-" +
            change.kind +
            "-" +
            index
          }
        >
          <span className="news-update-badge" data-kind={change.kind}>
            {change.kind_zh || change.kind}
          </span>
          此前：
          {change.previous_statement ??
            "未能读取此前命题（" + change.previous_ref?.slice(0, 16) + "…）"}
          {change.relation_zh ? <small> · {change.relation_zh}</small> : null}
        </p>
      ))}
    </div>
  );
}

function quantityText(claim: NewsClaim): string {
  return (claim.quantities ?? [])
    .map(
      (quantity) =>
        quantity.name +
        " " +
        quantity.value +
        quantity.unit +
        (quantity.period ? "（" + quantity.period + "）" : ""),
    )
    .join("；");
}

function ClaimItem({
  changes,
  claim,
  index,
  onNavigate,
  sourcePositions,
}: {
  changes: NewsClaimChange[];
  claim: NewsClaim;
  index: number;
  onNavigate: NewsDetailNavigate;
  sourcePositions: Map<string, number>;
}) {
  const counts = claim.relation_counts;
  const prior = changes.filter((change) => change.previous_ref);
  const kinds = Array.from(new Map(changes.map((change) => [change.kind, change])).values());
  const firstCitation = claim.citations[0];
  const moreCitations = claim.citations.slice(1);
  const sourcePosition = firstCitation
    ? sourcePositions.get(firstCitation.evidence_ref)
    : undefined;
  return (
    <li
      className="news-update-claim"
      data-retired={claim.retired || claim.superseded || undefined}
      id={"claim-record-" + (index + 1)}
      tabIndex={-1}
    >
      <span aria-hidden className="news-update-claim-number">
        {String(index + 1).padStart(2, "0")}
      </span>
      <div className="news-update-claim-main">
        <p className="news-update-badges">
          {kinds.map((change) => (
            <span className="news-update-badge" data-kind={change.kind} key={change.kind}>
              {change.kind_zh || change.kind}
            </span>
          ))}
          {claim.superseded ? (
            <span className="news-update-badge" data-kind="retired">
              已被后续变化替代
            </span>
          ) : null}
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
          {claim.polarity === "negative" ? (
            <span className="news-update-badge">{claim.polarity_zh || claim.polarity}</span>
          ) : null}
          <span className="news-update-claim-kind">
            {claim.content_kind_zh || claim.content_kind}
          </span>
        </p>
        <p className="news-update-statement">{claim.statement}</p>
        {sourcePosition !== undefined ? (
          <p className="news-update-claim-evidence">
            <button
              className="news-update-text-button"
              onClick={() => onNavigate(sourceTarget(sourcePosition))}
              type="button"
            >
              来源 {String(sourcePosition + 1).padStart(2, "0")}
              {firstCitation?.source ? " · " + firstCitation.source.publisher_id : ""}
            </button>
            {firstCitation?.source ? (
              <span>
                {firstCitation.source.source_authority_zh || firstCitation.source.source_authority}
              </span>
            ) : null}
          </p>
        ) : firstCitation?.source ? (
          <p className="news-update-claim-evidence">
            <SourceLabel source={firstCitation.source} />
          </p>
        ) : null}
        <div className="news-update-claim-actions">
          {prior.length ? (
            <details
              className="news-update-disclosure"
              id={"news-claim-history-" + (index + 1)}
              tabIndex={-1}
            >
              <summary>历史比较 {prior.length} 项</summary>
              <PriorComparisons changes={prior} />
            </details>
          ) : null}
          <details
            className="news-update-disclosure"
            id={"news-claim-details-" + (index + 1)}
            tabIndex={-1}
          >
            <summary>引用与命题详情</summary>
            {firstCitation ? (
              <blockquote className="news-update-quote">
                <p>{firstCitation.quote}</p>
                {firstCitation.source ? (
                  <footer>
                    <SourceLabel source={firstCitation.source} />
                  </footer>
                ) : null}
              </blockquote>
            ) : null}
            <FactGrid
              className="news-update-facts"
              facts={[
                { label: "表达方式", value: claim.mode_zh || claim.mode },
                { label: "角色", value: claim.actor_role_zh || claim.actor_role || "" },
                { label: "阶段", value: claim.phase_zh || claim.phase || "" },
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
                    .map((asset) => asset.symbol + (asset.role === "primary" ? "" : "（提及）"))
                    .join(" "),
                },
                {
                  label: "来源关系",
                  value: counts
                    ? [
                        counts.supports ? "支持 " + counts.supports : "",
                        counts.reports ? "转述 " + counts.reports : "",
                        counts.refutes ? "反驳 " + counts.refutes : "",
                        counts.unresolved ? "未判定 " + counts.unresolved : "",
                      ]
                        .filter(Boolean)
                        .join(" · ")
                    : "",
                },
                { label: "首次可用", value: absoluteTime(claim.first_available_at_ms) },
              ]}
              label="命题字段"
            />
          </details>
          {moreCitations.length ? (
            <details
              className="news-update-disclosure"
              id={"news-claim-citations-" + (index + 1)}
              tabIndex={-1}
            >
              <summary>其余引用 {moreCitations.length} 条</summary>
              {moreCitations.map((citation, citationIndex) => {
                const position = sourcePositions.get(citation.evidence_ref);
                return (
                  <blockquote
                    className="news-update-quote"
                    key={citation.evidence_ref + "-" + citationIndex}
                  >
                    <p>{citation.quote}</p>
                    <footer>
                      {position !== undefined ? (
                        <button
                          className="news-update-text-button"
                          onClick={() => onNavigate(sourceTarget(position))}
                          type="button"
                        >
                          来源 {String(position + 1).padStart(2, "0")}
                        </button>
                      ) : null}
                      {citation.source ? <SourceLabel source={citation.source} /> : null}
                    </footer>
                  </blockquote>
                );
              })}
            </details>
          ) : null}
        </div>
      </div>
    </li>
  );
}

function SourceLabel({ source }: { source: NewsUpdateSource }) {
  const url = validExternalUrl(source.url);
  return (
    <span className="news-update-source-label">
      <b>{sourceDisplayName(source, source.publisher_id)}</b>
      {source.attribution ? <span>{source.attribution}</span> : null}
      <span>{source.source_authority_zh || source.source_authority}</span>
      {source.published_at_ms != null ? (
        <time dateTime={new Date(source.published_at_ms).toISOString()}>
          {optionalTime(source.published_at_ms)}
        </time>
      ) : null}
      {url ? (
        <a href={url} rel="noreferrer" target="_blank">
          原文 <ExternalLink aria-hidden />
        </a>
      ) : null}
    </span>
  );
}

/** Current work and historical sends retain their own state, revision, and recorded body. */
export function NewsProcessingState({
  processing,
  update,
  onNavigate,
}: {
  processing?: NewsProcessing | null;
  update?: NewsEventUpdate | null;
  onNavigate: NewsDetailNavigate;
}) {
  if (!processing) return <EmptyNote>没有处理记录。</EmptyNote>;
  const { semantic, notification } = processing;
  const plan = notification?.plan;
  const decisions = plan?.claim_decisions ?? [];
  const intents = processing.intents ?? [];
  const claimPositions = new Map((update?.claims ?? []).map((claim, index) => [claim.ref, index]));
  const intentPositions = new Map(intents.map((intent, index) => [intent.intent_id, index]));
  return (
    <section aria-label="处理状态" className="news-detail-processing">
      <header className="news-update-section-head">
        <h2>处理记录</h2>
        <span>语义处理、通知选择与实际发送</span>
      </header>
      {processing.update_error_code ? (
        <p className="news-update-alert">
          已采用版本无法按当前合同解码：{processing.update_error_code}
        </p>
      ) : null}
      <div className="news-update-processing-summary">
        <div>
          <small>语义处理</small>
          <b>{semantic ? semantic.state_zh || semantic.state : "未记录"}</b>
          {semantic ? (
            <span>
              已完成 {semantic.done_revision ?? "—"} / 最新 {semantic.wanted_revision}
            </span>
          ) : null}
        </div>
        <div>
          <small>通知决定</small>
          <b>{notification ? notification.state_zh || notification.state : "未记录"}</b>
          {plan ? (
            <span>
              {plan.action_zh} · {plan.reason_zh}
            </span>
          ) : null}
        </div>
        <div>
          <small>发送记录</small>
          <b>{intents.length ? intents.length + " 条" : "无发送记录"}</b>
          <span>各次发送按原版本保留</span>
        </div>
      </div>
      {notification?.plan_error_code ? (
        <p className="news-update-alert">通知计划无法解码：{notification.plan_error_code}</p>
      ) : null}
      <details className="news-update-processing-details" id="processing-details" tabIndex={-1}>
        <summary>语义处理详情</summary>
        <FactGrid
          facts={[
            { label: "语义处理", value: semantic ? semantic.state_zh || semantic.state : "未记录" },
            {
              label: "材料版本",
              value: semantic
                ? "已完成 " +
                  (semantic.done_revision ?? "—") +
                  " / 最新 " +
                  semantic.wanted_revision
                : "",
            },
            { label: "尝试次数", value: semantic ? String(semantic.attempts ?? 0) : "" },
            { label: "最近结果", value: semantic?.last_outcome ?? "" },
            { label: "错误", value: semantic?.last_error_code ?? "" },
            { label: "补读", value: semantic?.extra_read_state_zh ?? "" },
          ]}
          label="语义处理状态"
        />
      </details>
      <details className="news-update-processing-details" id="decision-record" tabIndex={-1}>
        <summary>
          逐条通知决定 <span>{decisions.length} 条命题</span>
        </summary>
        <FactGrid
          facts={[
            {
              label: "通知",
              value: notification ? notification.state_zh || notification.state : "未记录",
            },
            {
              label: "通知尝试次数",
              value: notification ? String(notification.attempts ?? 0) : "",
            },
            { label: "通知错误", value: notification?.last_error_code ?? "" },
            { label: "通知决定", value: plan ? plan.action_zh + " · " + plan.reason_zh : "" },
            { label: "重点", value: plan ? (plan.key ? "是" : "否") : "" },
          ]}
          label="通知状态"
        />
        {decisions.length ? (
          <section aria-label="逐条通知决定">
            <ul className="news-update-decisions">
              {decisions.map((row) => {
                const position = claimPositions.get(row.claim_ref);
                const earlierIntent = row.earlier_intent_id
                  ? intentPositions.get(row.earlier_intent_id)
                  : undefined;
                return (
                  <li data-decision={row.decision} key={row.claim_ref}>
                    <div className="news-update-decision-heading">
                      {position !== undefined ? (
                        <button
                          className="news-update-text-button"
                          onClick={() => onNavigate(claimTarget(position))}
                          type="button"
                        >
                          命题 {String(position + 1).padStart(2, "0")}
                        </button>
                      ) : null}
                      <span className="news-update-badge" data-kind={row.decision}>
                        {row.decision_zh || row.decision}
                      </span>
                      {row.novelty_zh ? (
                        <span className="news-update-badge" data-kind={row.novelty ?? undefined}>
                          {row.novelty_zh}
                        </span>
                      ) : null}
                      {row.report_kind ? (
                        <small>报道类型：{row.report_kind_zh || row.report_kind.value}</small>
                      ) : null}
                    </div>
                    <p>{row.reason_zh || row.reason}</p>
                    <small>{row.statement ?? row.claim_ref}</small>
                    {row.report_kind || row.historical_judgment ? (
                      <details>
                        <summary>
                          {row.historical_judgment
                            ? "历史模型证据（只读）"
                            : "模型证据与冻结策略概率"}
                        </summary>
                        <pre className="news-update-json">
                          {JSON.stringify(
                            row.historical_judgment ?? {
                              report_kind: row.report_kind,
                              materiality: row.materiality,
                              interrupt: row.interrupt,
                              anchor: row.anchor,
                              e: row.e,
                              m: row.m,
                              i: row.i,
                              p_push: row.p_push,
                              p_key: row.p_key,
                              held: row.held,
                              certification_status: row.certification_status,
                            },
                            null,
                            2,
                          )}
                        </pre>
                      </details>
                    ) : null}
                    {earlierIntent !== undefined ? (
                      <button
                        className="news-update-text-button"
                        onClick={() => onNavigate("news-intent-" + (earlierIntent + 1))}
                        type="button"
                      >
                        查看关联发送记录
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : (
          <EmptyNote>没有逐条通知决定记录。</EmptyNote>
        )}
      </details>
      <details className="news-update-processing-details" id="delivery-record" tabIndex={-1}>
        <summary>
          发送记录 <span>{intents.length} 条</span>
        </summary>
        <section aria-label="发送记录">
          {intents.length ? (
            <ul className="news-update-intents">
              {intents.map((intent, index) => (
                <li id={"news-intent-" + (index + 1)} key={intent.intent_id} tabIndex={-1}>
                  <p className="news-update-intent-heading">
                    <span className="news-update-badge" data-kind={intent.state}>
                      {intent.state_zh || intent.state}
                    </span>
                    {intent.key ? <span className="news-update-badge">重点</span> : null}
                    <b>{intent.headline_zh ?? (intent.claim_refs?.length ?? 0) + " 条命题"}</b>
                  </p>
                  <p className="news-update-intent-meta">
                    <span>
                      {optionalTime(
                        intent.settled_at_ms ?? intent.attempted_at_ms ?? intent.enqueued_at_ms,
                      )}
                    </span>
                    <span>
                      内容版本：<code>{intent.content_revision ?? "未记录"}</code>
                    </span>
                  </p>
                  {intent.error_code ? (
                    <p className="news-update-alert">错误：{intent.error_code}</p>
                  ) : null}
                  {intent.state === "ambiguous" ? (
                    <p className="news-update-alert">发送结果不明，不能确认已送达。</p>
                  ) : null}
                  {intent.body ? (
                    <details
                      className="news-update-disclosure"
                      id={"news-intent-body-" + (index + 1)}
                      tabIndex={-1}
                    >
                      <summary>{intent.state === "sent" ? "实际发送正文" : "冻结意图正文"}</summary>
                      <pre className="news-update-intent-body">{intent.body}</pre>
                    </details>
                  ) : (
                    <p className="news-update-section-note">没有记录正文。</p>
                  )}
                  {intent.receipt ? (
                    <details
                      className="news-update-disclosure"
                      id={"news-intent-receipt-" + (index + 1)}
                      tabIndex={-1}
                    >
                      <summary>提供商回执</summary>
                      <pre className="news-update-intent-body">
                        {JSON.stringify(intent.receipt, null, 2)}
                      </pre>
                    </details>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyNote>还没有发送意图。</EmptyNote>
          )}
        </section>
      </details>
    </section>
  );
}
