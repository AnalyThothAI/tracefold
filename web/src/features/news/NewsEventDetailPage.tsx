import { newsPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ExternalLink } from "lucide-react";

import "./news.css";
import "./newsDetail.css";
import { NewsGroundedAssets, OpenNewsScoreBadge } from "./NewsEventRow";
import { NewsSectionTabs } from "./NewsSectionTabs";
import { NewsVerdictPanel } from "./NewsVerdictPanel";
import {
  absoluteTime,
  admissionLabel,
  assetClassLabel,
  deliveryStateLabel,
  displayTime,
  familyLabel,
  optionalTime,
  priorityLabel,
  validExternalUrl,
} from "./newsLabels";
import {
  type NewsDelivery,
  type NewsEvent,
  type NewsEventDetail,
  type NewsEventMember,
  type NewsLabel,
  useNewsEventWithToken,
} from "./useNewsPage";

export function NewsEventDetailPage({ eventId, token }: { eventId: string; token: string }) {
  const query = useNewsEventWithToken(token, eventId);
  const detail = query.data;
  return (
    <section
      aria-label="新闻事件详情"
      className="news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <NewsSectionTabs active="event" />
      <header className="news-toolbar">
        <RouteBackLink ariaLabel="返回新闻事件流" label="返回事件流" to={newsPath()} />
        <span className="news-live-state">{query.isFetching ? "正在刷新" : "证据已保存"}</span>
      </header>
      {query.isLoading && !detail ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取事件详情" />
      ) : null}
      {query.isError && !detail ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {detail ? <EventDocument detail={detail} /> : null}
    </section>
  );
}

function EventDocument({ detail }: { detail: NewsEventDetail }) {
  const { event } = detail;
  const triageVerdict = detail.verdicts.find((verdict) => verdict.stage === "triage")?.verdict as
    | { title_zh?: string }
    | undefined;
  const displayTitle = triageVerdict?.title_zh?.trim() || event.leader_title;
  return (
    <article className="news-event-detail">
      <EventHero displayTitle={displayTitle} event={event} />
      <section
        aria-labelledby="news-members-heading"
        className="news-detail-card news-members-section"
      >
        <header>
          <div>
            <span className="news-eyebrow">MEMBERS</span>
            <h2 id="news-members-heading">{event.member_count} 条报道</h2>
          </div>
          <span>最近报道 {absoluteTime(event.last_member_at_ms)}</span>
        </header>
        <div className="news-member-list">
          {detail.members.map((member) => (
            <MemberCard key={member.item_id} member={member} />
          ))}
        </div>
      </section>
      <NewsVerdictPanel verdicts={detail.verdicts} />
      <section
        aria-labelledby="news-deliveries-heading"
        className="news-detail-card news-deliveries-section"
      >
        <header>
          <div>
            <span className="news-eyebrow">DELIVERIES</span>
            <h2 id="news-deliveries-heading">推送记录</h2>
          </div>
          <span>{detail.deliveries.length} 条</span>
        </header>
        {detail.deliveries.length ? (
          <ol className="news-delivery-list">
            {detail.deliveries.map((delivery) => (
              <li key={`${delivery.kind}:${delivery.attempted_at_ms}`}>
                <DeliveryRow delivery={delivery} />
              </li>
            ))}
          </ol>
        ) : (
          <p className="news-detail-empty">尚未产生推送。</p>
        )}
      </section>
      <section
        aria-labelledby="news-labels-heading"
        className="news-detail-card news-labels-section"
      >
        <header>
          <div>
            <span className="news-eyebrow">LABELS</span>
            <h2 id="news-labels-heading">操作者标注</h2>
          </div>
          <span>{detail.labels?.length ?? 0} 条</span>
        </header>
        <LabelsList labels={detail.labels ?? []} />
      </section>
    </article>
  );
}

function EventHero({ displayTitle, event }: { displayTitle: string; event: NewsEvent }) {
  const originalUrl = validExternalUrl(event.leader_url);
  const description = validDescription(event.leader_description, displayTitle);
  return (
    <header className="news-event-hero" data-priority={event.priority}>
      <div className="news-event-badges">
        <span data-priority={event.priority}>{priorityLabel(event.priority)}</span>
        <span>{admissionLabel(event.admission)}</span>
        <span>{familyLabel(event.family)}</span>
        <span>{assetClassLabel(event.asset_class)}</span>
        <span>{event.engine_type}</span>
        <OpenNewsScoreBadge score={event.provider_score_max} />
      </div>
      <h1>{displayTitle}</h1>
      {event.leader_title.trim() !== displayTitle.trim() ? (
        <p className="news-original-title">
          <span>原标题</span>
          {event.leader_title}
        </p>
      ) : null}
      {description ? <p className="news-event-lead">{description}</p> : null}
      {event.context_line.trim() ? (
        <p className="news-event-context-note">
          <span>Storyline</span>
          {event.context_line}
        </p>
      ) : null}
      <div className="news-event-support">
        <NewsGroundedAssets assets={event.grounded_assets} hits={event.watchlist_hits} />
      </div>
      <dl className="news-event-facts">
        <Fact label="来源" value={event.reporting_origin} />
        <Fact label="开启时间" value={displayTime(event.opened_at_ms)} />
        <Fact label="最近报道" value={displayTime(event.last_member_at_ms)} />
        <Fact label="发布时间" value={optionalTime(event.published_at_ms)} />
        <Fact label="接入模式" value={event.ingest_mode} />
        <Fact label="Storyline key" value={event.storyline_key} />
        <Fact label="宏观词库" value={event.macro_lexicon ? "命中" : "未命中"} />
        <Fact label="Event ID" value={event.event_id} />
      </dl>
      {originalUrl ? (
        <a className="news-primary-link" href={originalUrl} rel="noreferrer" target="_blank">
          阅读代表原文
          <ExternalLink aria-hidden />
        </a>
      ) : null}
    </header>
  );
}

function MemberCard({ member }: { member: NewsEventMember }) {
  const url = validExternalUrl(member.url);
  const summary = validDescription(member.description, member.title);
  return (
    <article className="news-member-card" data-match-kind={member.match_kind}>
      <header>
        <b>{member.reporting_origin}</b>
        <span>{member.match_kind}</span>
        {member.jaccard_estimate != null ? (
          <span>Jaccard {member.jaccard_estimate.toFixed(3)}</span>
        ) : null}
        <time dateTime={new Date(member.published_at_ms).toISOString()}>
          {absoluteTime(member.published_at_ms)}
        </time>
      </header>
      <h3>{member.title}</h3>
      {summary ? <p>{summary}</p> : null}
      <div className="news-member-actions">
        <code>{member.item_id}</code>
        {url ? (
          <a href={url} rel="noreferrer" target="_blank">
            查看原文
            <ExternalLink aria-hidden />
          </a>
        ) : null}
      </div>
    </article>
  );
}

function DeliveryRow({ delivery }: { delivery: NewsDelivery }) {
  const receiptEntries = Object.entries(delivery.receipt ?? {});
  return (
    <article className="news-delivery-row" data-state={delivery.state}>
      <header>
        <span className="news-delivery-kind">{delivery.kind}</span>
        <span className="news-delivery-state" data-state={delivery.state}>
          {deliveryStateLabel(delivery.state)}
        </span>
        {delivery.error_code ? <code>{delivery.error_code}</code> : null}
      </header>
      <dl>
        <Fact label="尝试时间" value={absoluteTime(delivery.attempted_at_ms)} />
        <Fact label="落定时间" value={optionalTime(delivery.settled_at_ms)} />
        {receiptEntries.map(([key, value]) => (
          <Fact key={key} label={`回执 ${key}`} value={compactValue(value)} />
        ))}
      </dl>
    </article>
  );
}

function LabelsList({ labels }: { labels: readonly NewsLabel[] }) {
  if (!labels.length) {
    return <p className="news-detail-empty">尚无标注；用 `tracefold news label` 记录。</p>;
  }
  return (
    <ul className="news-label-list">
      {labels.map((label) => (
        <li key={`${label.source}:${label.created_at_ms}`}>
          <span className="news-label-source">{label.source}</span>
          <span className="news-label-value">{compactValue(label.label)}</span>
          <span className="news-label-time">{absoluteTime(label.created_at_ms)}</span>
        </li>
      ))}
    </ul>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function validDescription(value: string | null | undefined, title: string): string | null {
  const description = value?.trim() ?? "";
  if (!description || description === title.trim() || description.length < 8) return null;
  if (/^(?:n\/?a|none|null|undefined|no description)$/i.test(description)) return null;
  return description;
}

function compactValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
