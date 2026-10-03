import { newsEventPath } from "@shared/routing/paths";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";

import type { NewsClaimDecision, NewsEventDetail, NewsUpdateIntent } from "../../api/newsQueries";
import {
  annotatedSource,
  eventReader,
  eventTiming,
  seconds,
  sourceDisplayName,
} from "../../model/eventReader";
import { absoluteTime, clockTime, outcomeReason, validExternalUrl } from "../../model/newsLabels";

import type { NewsDetailNavigate } from "./NewsEventUpdate";

type EarlierMessage = NonNullable<NewsClaimDecision["earlier"]>;
type Duplicate = NonNullable<NonNullable<NewsEventDetail["event_update"]>["duplicates"]>[number];

function OriginalEvent({ original }: { original: Duplicate }) {
  return (
    <div className="news-reader-earlier">
      <p>
        同一事实最早见于
        {original.first_available_at_ms != null
          ? ` · ${absoluteTime(original.first_available_at_ms)}`
          : "更早事件"}
        {original.reporting_origin ? ` · ${original.reporting_origin}` : ""}
      </p>
      {original.event_id ? (
        <Link to={newsEventPath(original.event_id)}>{original.headline || "查看原事件"}</Link>
      ) : (
        <p>原事件暂不可读取。</p>
      )}
      <p>
        原条状态：
        {original.received_at_ms != null
          ? `已推送 · ${clockTime(original.received_at_ms)} 送达`
          : `未推送 · ${original.reason_zh}${original.decided_at_ms != null ? `（${clockTime(original.decided_at_ms)}）` : ""}`}
      </p>
    </div>
  );
}

function carriedNote(detail: NewsEventDetail): string {
  const notification = detail.processing?.notification;
  if (!notification?.carried) return "";
  const names = notification.added_sources ?? [];
  const when =
    notification.carried_at_ms == null ? "" : `${clockTime(notification.carried_at_ms)} `;
  const source = names.length
    ? `新增 ${names.length} 个来源（${names.join("、")}）`
    : "仅非通知变化";
  const decision =
    notification.decided_at_ms == null ? "原有" : clockTime(notification.decided_at_ms);
  return eventReader(detail).currentSent.length
    ? `${when}${source}，事实没有变化，不再推送。这张卡片仍是读者收到的最新内容。`
    : `${when}${source}，事实没有变化，沿用 ${decision} 的判断，不重新判断。`;
}

function deliveryReason(detail: NewsEventDetail): string {
  const outcome = detail.outcome;
  return detail.processing?.notification?.carried &&
    outcome.kind === "not_notified" &&
    outcome.reason_at_ms != null
    ? `${outcome.reason_before_time_zh.replace(/ · $/, "")}（${clockTime(outcome.reason_at_ms)} 判断）`
    : outcomeReason(outcome);
}

function EarlierNotification({ earlier }: { earlier: EarlierMessage }) {
  return (
    <div className="news-reader-earlier">
      <p>
        读者此前已收到 ·{" "}
        <time dateTime={new Date(earlier.received_at_ms).toISOString()}>
          {absoluteTime(earlier.received_at_ms)}
        </time>
      </p>
      <Link to={newsEventPath(earlier.event_id)}>{earlier.headline_zh || "查看此前推送"}</Link>
      <p className="news-reader-body">{earlier.body}</p>
    </div>
  );
}

function SentCard({
  intent,
  detail,
  onNavigate,
}: {
  intent: NewsUpdateIntent;
  detail: NewsEventDetail;
  onNavigate: NewsDetailNavigate;
}) {
  const positions = new Map(
    (detail.event_update?.claims ?? []).map((claim, index) => [claim.ref, index + 1]),
  );
  return (
    <article className="news-reader-card">
      <div className="news-reader-meta">
        <span>新闻推送{intent.key ? " · 重点" : ""}</span>
        {intent.settled_at_ms != null ? (
          <time dateTime={new Date(intent.settled_at_ms).toISOString()}>
            {absoluteTime(intent.settled_at_ms)} 送达
          </time>
        ) : null}
      </div>
      {intent.lines?.length ? (
        <>
          <h3>{intent.headline_zh}</h3>
          <ol className="news-reader-card-lines">
            {intent.lines.map((line) => {
              const number = positions.get(line.claim_ref);
              return (
                <li key={line.claim_ref}>
                  {number ? (
                    <button
                      aria-label={`查看事实 ${number}`}
                      onClick={() => onNavigate(`news-claim-${number}`)}
                      type="button"
                    >
                      {number}
                    </button>
                  ) : (
                    <span aria-label="此前版本事实">—</span>
                  )}
                  <p>{line.text_zh}</p>
                </li>
              );
            })}
          </ol>
        </>
      ) : (
        <p className="news-reader-body">{intent.body || "已记录送达，未保留可展示的正文。"}</p>
      )}
    </article>
  );
}

export function NewsReaderDelivery({
  detail,
  onNavigate,
}: {
  detail: NewsEventDetail;
  onNavigate: NewsDetailNavigate;
}) {
  const reader = eventReader(detail);
  const earlier = new Map(
    reader.facts.flatMap((fact) =>
      fact.decision?.earlier
        ? [[fact.decision.earlier.intent_id, fact.decision.earlier] as const]
        : [],
    ),
  );
  const currentIds = new Set(reader.currentSent.map((intent) => intent.intent_id));
  const historical = reader.sent.filter((intent) => !currentIds.has(intent.intent_id));
  const duplicates =
    detail.outcome.kind === "duplicate" ? (detail.event_update?.duplicates ?? []) : [];
  return (
    <section
      aria-label="读者收到的推送"
      className="news-reader-section"
      id="reader-delivery"
      tabIndex={-1}
    >
      <h2>读者收到的推送</h2>
      {reader.currentSent.length ? (
        reader.currentSent.map((intent) => (
          <SentCard
            detail={detail}
            intent={intent}
            key={intent.intent_id}
            onNavigate={onNavigate}
          />
        ))
      ) : detail.outcome.kind === "duplicate" ? (
        <>
          <p className="news-reader-empty">本条不单独推送：内容与更早的事件相同</p>
          {duplicates.map((original) => (
            <OriginalEvent key={original.claim_ref} original={original} />
          ))}
          {!duplicates.length ? (
            <p className="news-reader-note">与更早事件重复，原事件暂不可读取。</p>
          ) : null}
          <p className="news-reader-note">
            复述不会重新判断，也不会单独推送；读者是否收到以原条为准。
          </p>
        </>
      ) : (
        <>
          <p className="news-reader-empty">
            {detail.outcome.group === "pending"
              ? "本次尚未确认送达"
              : detail.outcome.kind === "not_notified"
                ? "本次未推送"
                : "本次没有确认送达的推送"}
          </p>
          <p className="news-reader-note">{deliveryReason(detail)}</p>
          {Array.from(earlier.values()).map((message) => (
            <EarlierNotification earlier={message} key={message.intent_id} />
          ))}
        </>
      )}
      {carriedNote(detail) ? (
        <p className="news-reader-note news-reader-carried">{carriedNote(detail)}</p>
      ) : null}
      {historical.length ? (
        <details className="news-detail-record">
          <summary>此前版本已送达 · {historical.length} 条</summary>
          <p className="news-reader-note">这些是历史发送正文，不代表最新内容已送达。</p>
          {historical.map((intent) => (
            <SentCard
              detail={detail}
              intent={intent}
              key={intent.intent_id}
              onNavigate={onNavigate}
            />
          ))}
        </details>
      ) : null}
    </section>
  );
}

export function NewsReaderFacts({
  detail,
  onNavigate,
}: {
  detail: NewsEventDetail;
  onNavigate: NewsDetailNavigate;
}) {
  const { facts, currentSent, sentRefs } = eventReader(detail);
  const sources = new Map(
    (detail.event_update?.sources ?? []).map((source, index) => [source.evidence_ref, index + 1]),
  );
  const sentCount = facts.filter((fact) => fact.sent).length;
  return (
    <section
      aria-label="每件事与推送原因"
      className="news-reader-section"
      id="news-content"
      tabIndex={-1}
    >
      <h2>
        {detail.event_update
          ? `这条新闻说了 ${facts.length} 件事${
              sentCount
                ? `，推送了 ${sentCount} 件`
                : detail.outcome.kind === "duplicate"
                  ? "，与更早事件重复"
                  : detail.outcome.kind === "not_notified"
                    ? "，未推送"
                    : "，尚无确认送达"
            }`
          : "尚无已采用事实"}
      </h2>
      {!facts.length ? (
        <EmptyNote>尚无已采用事实，请先查看原文与处理状态。</EmptyNote>
      ) : (
        <ol className="news-reader-facts">
          {facts.map(({ claim, number, decision, text, sent, duplicate }) => {
            const pending = detail.processing?.intents?.find(
              (intent) =>
                intent.content_revision === detail.event_update?.content_revision &&
                intent.claim_refs?.includes(claim.ref) &&
                intent.state !== "sent",
            );
            const status = claim.retired
              ? "已撤回"
              : claim.superseded
                ? "已被后续变化替代"
                : sent
                  ? `已推送${decision?.render === "increment" ? " · 补充" : decision?.render === "correction" ? " · 更正" : ""}`
                  : duplicate
                    ? `重复 · ${duplicate.received_at_ms != null ? "原条已推送" : "未推送"}`
                    : pending
                      ? pending.state_zh || pending.state
                      : decision?.reason === "known_to_reader"
                        ? "读者已知 · 未推送"
                        : decision?.decision === "not_notified"
                          ? "未推送"
                          : decision?.decision === "deferred"
                            ? "等待判断"
                            : decision?.decision === "notify"
                              ? "决定推送 · 尚未确认送达"
                              : "尚无推送决定";
            return (
              <li
                className="news-reader-fact"
                data-sent={sent || undefined}
                id={`news-claim-${number}`}
                key={claim.ref}
                tabIndex={-1}
              >
                <span className="news-reader-number" aria-hidden>
                  {number}
                </span>
                <div>
                  <p className="news-reader-fact-state">
                    {status}
                    {sent && currentSent[0]?.settled_at_ms != null
                      ? ` · ${clockTime(currentSent[0].settled_at_ms)}`
                      : ""}
                    {claim.disputed ? " · 来源有分歧" : ""}
                  </p>
                  <h3>{text || claim.statement}</h3>
                  {text ? <p className="news-reader-original">{claim.statement}</p> : null}
                  <p className="news-reader-reason">
                    {duplicate
                      ? `与${duplicate.first_available_at_ms == null ? "更早" : ` ${clockTime(duplicate.first_available_at_ms)} `}事件中的事实等价（复述）；${duplicate.received_at_ms != null ? "读者已收到原条" : duplicate.reason_zh === "旧版模型判断：只进信息流" ? "原条只进信息流" : "原条未推送"}，本条沿用，不再推送。`
                      : decision?.reason === "stale_source"
                        ? `同一事实 ${absoluteTime(claim.first_available_at_ms)} 已出现（超过 3 小时）`
                        : decision?.reason_zh ||
                          (claim.retired || claim.superseded
                            ? "这一事实已不再作为当前内容。"
                            : "尚未记录这件事的推送判断。")}
                  </p>
                  {duplicate?.event_id ? (
                    <p className="news-reader-prior-link">
                      <Link to={newsEventPath(duplicate.event_id)}>查看原事件 ↗</Link>
                    </p>
                  ) : null}
                  {decision?.earlier ? (
                    <p className="news-reader-prior-link">
                      读者 {absoluteTime(decision.earlier.received_at_ms)} 已收到{" "}
                      <Link to={newsEventPath(decision.earlier.event_id)}>
                        《{decision.earlier.headline_zh || "此前推送"}》
                      </Link>
                      {decision.render === "increment"
                        ? "，本条补充其中的新细节。"
                        : decision.render === "correction"
                          ? "，本条更正此前内容。"
                          : "。"}
                    </p>
                  ) : decision?.earlier_intent_id ? (
                    <p className="news-reader-note">
                      有关联的此前推送，摘要暂不可读；工程细节保留其记录标识。
                    </p>
                  ) : null}
                  {decision?.report_kind ? (
                    <div className="news-reader-score">
                      <p>报道类型：{decision.report_kind_zh || decision.report_kind.value}</p>
                      {decision.materiality_probabilities?.length === 4 ? (
                        <p aria-label="新增影响程度分布">
                          新增影响：
                          {decision.materiality_probabilities
                            .map(
                              (probability, index) =>
                                `${["可忽略", "有限", "明确", "大盘"][index]} ${(probability * 100).toFixed(0)}%`,
                            )
                            .join(" · ")}
                        </p>
                      ) : null}
                      {decision.p_push != null && decision.p_key != null ? (
                        <p>
                          {decision.certification_status === "certified" ? "已认证" : "未认证候选"}
                          推送概率 {(decision.p_push * 100).toFixed(0)}%
                          {decision.push_cut != null
                            ? `（推送线 ${(decision.push_cut * 100).toFixed(0)}%）`
                            : "（推送线未记录）"}
                          {detail.processing?.notification?.decided_at_ms != null
                            ? ` · ${clockTime(detail.processing.notification.decided_at_ms)} 判断`
                            : ""}
                          {` · 重点概率 ${(decision.p_key * 100).toFixed(0)}%`}
                          {decision.key_cut != null
                            ? `（重点线 ${(decision.key_cut * 100).toFixed(0)}%）`
                            : ""}
                        </p>
                      ) : null}
                    </div>
                  ) : decision?.historical_judgment ? (
                    <small className="news-reader-score">历史判断按原记录保留，仅供查看。</small>
                  ) : null}
                  <div className="news-reader-citations">
                    {Array.from(
                      new Set(
                        claim.citations
                          .map((citation) => sources.get(citation.evidence_ref))
                          .filter((position): position is number => position != null),
                      ),
                    ).map((position) => (
                      <button
                        key={position}
                        onClick={() => onNavigate(`source-${String(position).padStart(2, "0")}`)}
                        type="button"
                      >
                        查看原文 {position} ↗
                      </button>
                    ))}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}
      {currentSent.length && sentRefs.size > sentCount ? (
        <p className="news-reader-note">发送记录还包含当前版本未保留的事实，请以冻结正文为准。</p>
      ) : null}
    </section>
  );
}

export function NewsReaderSources({
  detail,
  onNavigate,
}: {
  detail: NewsEventDetail;
  onNavigate: NewsDetailNavigate;
}) {
  const update = detail.event_update;
  const sources = update?.sources ?? [];
  return (
    <section aria-label="原文" className="news-reader-section" id="news-source" tabIndex={-1}>
      <h2>原文</h2>
      <p className="news-reader-note">
        {update
          ? "编号对应上面的事实；只标记原文中能精确匹配的引用。"
          : "尚无可用于标注的已采用事实；以下展示来源记录。"}
      </p>
      {sources.length ? (
        sources.map((item, index) => {
          const url = validExternalUrl(item.source.url);
          return (
            <article
              className="news-reader-source"
              id={`source-${String(index + 1).padStart(2, "0")}`}
              key={item.evidence_ref}
              tabIndex={-1}
            >
              <div className="news-reader-meta">
                <b>{sourceDisplayName(item.source, item.source.publisher_id)}</b>
                {item.source.published_at_ms != null ? (
                  <time dateTime={new Date(item.source.published_at_ms).toISOString()}>
                    {absoluteTime(item.source.published_at_ms)}
                  </time>
                ) : null}
                {url ? (
                  <a href={url} rel="noreferrer" target="_blank">
                    打开原文 <ExternalLink aria-hidden />
                  </a>
                ) : null}
              </div>
              <p className="news-reader-source-text">
                {annotatedSource(item.text, item.evidence_ref, update?.claims ?? []).map(
                  (part, partIndex) =>
                    part.numbers.length ? (
                      <mark key={partIndex}>
                        {part.text}
                        <sup>
                          {part.numbers.map((number) => (
                            <button
                              aria-label={`查看事实 ${number}`}
                              key={number}
                              onClick={() => onNavigate(`news-claim-${number}`)}
                              type="button"
                            >
                              {number}
                            </button>
                          ))}
                        </sup>
                      </mark>
                    ) : (
                      <span key={partIndex}>{part.text}</span>
                    ),
                )}
              </p>
              {item.text_truncated ? (
                <p className="news-reader-note">
                  已保存的来源正文较长，此处为节选；未显示的文字不做引用匹配。
                </p>
              ) : null}
              <p className="news-reader-source-links">
                相关事实：
                {(update?.claims ?? []).flatMap((claim, position) =>
                  claim.citations.some((citation) => citation.evidence_ref === item.evidence_ref)
                    ? [
                        <button
                          key={claim.ref}
                          onClick={() => onNavigate(`news-claim-${position + 1}`)}
                          type="button"
                        >
                          {position + 1}
                        </button>,
                      ]
                    : [],
                )}
              </p>
            </article>
          );
        })
      ) : (
        <>
          {detail.members.map((member) => (
            <article className="news-reader-source" key={member.item_id}>
              <div className="news-reader-meta">
                <b>{member.reporting_origin || "来源未记录"}</b>
                <time>{absoluteTime(member.published_at_ms)}</time>
                {validExternalUrl(member.url) ? (
                  <a href={validExternalUrl(member.url)!} rel="noreferrer" target="_blank">
                    打开原文 <ExternalLink aria-hidden />
                  </a>
                ) : null}
              </div>
              <h3>{member.title}</h3>
              <p className="news-reader-source-text">
                {member.description || member.fact_text || "没有保存正文。"}
              </p>
            </article>
          ))}
          {!detail.members.length ? <EmptyNote>没有来源正文记录。</EmptyNote> : null}
        </>
      )}
    </section>
  );
}

export function NewsReaderStory({ detail }: { detail: NewsEventDetail }) {
  const story = detail.story;
  return (
    <section aria-label="同一个故事" className="news-reader-section" id="news-story" tabIndex={-1}>
      <h2>同一个故事</h2>
      {story?.events?.length ? (
        <>
          <p className="news-reader-note">同一故事线 · 本条收到时间前后各 24 小时</p>
          <ol className="news-reader-story">
            {story.events.map((event) => (
              <li
                aria-current={event.event_id === detail.event.event_id ? "true" : undefined}
                key={event.event_id}
              >
                <time>{absoluteTime(event.published_at_ms ?? event.opened_at_ms)}</time>
                <div>
                  {event.event_id === detail.event.event_id ? (
                    <b>本条 · {event.headline}</b>
                  ) : (
                    <Link to={newsEventPath(event.event_id)}>{event.headline}</Link>
                  )}
                  <p>
                    {sourceDisplayName(null, event.reporting_origin || "来源未记录")} ·{" "}
                    {event.outcome.text_zh}
                    {event.received_at_ms != null ? ` ${absoluteTime(event.received_at_ms)}` : ""}
                  </p>
                  <small>{outcomeReason(event.outcome)}</small>
                </div>
              </li>
            ))}
          </ol>
          {story.has_more ? (
            <p className="news-reader-note">显示本条及最近 29 条，窗口内还有其他故事线记录。</p>
          ) : null}
          <p className="news-reader-note">
            故事线相同不代表同一事实；具体补充与更正以上方的已记录关系为准。
          </p>
        </>
      ) : (
        <EmptyNote>没有可展示的故事线记录。</EmptyNote>
      )}
    </section>
  );
}

export function NewsReaderTiming({ detail }: { detail: NewsEventDetail }) {
  const timing = eventTiming(detail);
  return (
    <section aria-label="处理用时" className="news-reader-section">
      <h2>用时{timing.elapsed != null ? ` ${seconds(timing.elapsed)}` : " · 尚无完成记录"}</h2>
      <p className="news-reader-note">
        {timing.end == null
          ? "未记录完成时刻，暂不展示总耗时。"
          : timing.completed
            ? "从收到本事件到有效决定的最后一次确认送达。"
            : "从收到本事件到本次通知决定；没有确认送达。"}
      </p>
      {timing.parts.length ? (
        <ul className="news-reader-timing">
          {timing.parts.map((part) => (
            <li key={part.label}>
              <span>{part.label}</span>
              <b>{seconds(part.ms)}</b>
            </li>
          ))}
        </ul>
      ) : null}
      <p className="news-reader-note">
        分段只展示保存的测量值，缺失或未归属到本次发送的阶段不补算。
      </p>
    </section>
  );
}
