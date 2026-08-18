import { newsEventPath } from "@shared/routing/paths";
import { ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";

import "./newsEventRow.css";
import {
  admissionLabel,
  assetClassLabel,
  decisionLabel,
  deliveryStateLabel,
  directionLabel,
  displayTime,
  familyLabel,
  formatPoints,
  magnitudeLabel,
  priorityLabel,
  validExternalUrl,
} from "./newsLabels";
import type { NewsDeliverySummary, NewsFeedEvent, NewsTriageSummary } from "./useNewsPage";

export function NewsEventRow({ event }: { event: NewsFeedEvent }) {
  const title = event.title_zh?.trim() || event.leader_title;
  const showLeaderTitle = event.leader_title.trim() !== title.trim();
  const originalUrl = validExternalUrl(event.leader_url);
  const contextLine = validContextLine(event.context_line, title);
  const triage = event.triage ?? null;
  return (
    <article
      className="news-event-row"
      data-decision={triage?.final_decision ?? "none"}
      data-direction={triage?.direction ?? "none"}
      data-event-id={event.event_id}
      data-priority={event.priority}
    >
      <div className="news-event-primary">
        <header className="news-event-meta">
          <span className="news-event-classification">
            <span className="news-event-priority" data-priority={event.priority}>
              {priorityLabel(event.priority)}
            </span>
            {triage ? (
              <span className="news-event-decision" data-decision={triage.final_decision}>
                {decisionLabel(triage.final_decision)}
                {triage.degraded ? <small>降级</small> : null}
              </span>
            ) : (
              <span className="news-event-decision" data-decision="pending">
                待判定
              </span>
            )}
            <span className="news-event-admission" data-admission={event.admission}>
              {admissionLabel(event.admission)}
            </span>
            <span className="news-event-family">{familyLabel(event.family)}</span>
            <span className="news-event-asset-class" data-asset-class={event.asset_class}>
              {assetClassLabel(event.asset_class)}
            </span>
            <OpenNewsScoreBadge score={event.provider_score_max} />
          </span>
          <span className="news-event-context">
            <span>{event.reporting_origin}</span>
            <time dateTime={new Date(event.opened_at_ms).toISOString()}>
              {displayTime(event.opened_at_ms)}
            </time>
            <span>{event.member_count} 条报道</span>
          </span>
        </header>
        <Link className="news-event-title" to={newsEventPath(event.event_id)}>
          <h2>{title}</h2>
        </Link>
        {showLeaderTitle ? (
          <p className="news-event-leader-title">
            <span>原标题</span>
            {event.leader_title}
          </p>
        ) : null}
        {contextLine ? <p className="news-event-context-line">{contextLine}</p> : null}
        <footer className="news-event-footer">
          <NewsGroundedAssets assets={event.grounded_assets} hits={event.watchlist_hits} />
          <NewsTriageStrip triage={triage} />
          <NewsDeliveryState delivery={event.delivery ?? null} />
          {originalUrl ? (
            <a className="news-original-link" href={originalUrl} rel="noreferrer" target="_blank">
              查看原文
              <ExternalLink aria-hidden />
            </a>
          ) : null}
        </footer>
      </div>
    </article>
  );
}

export function OpenNewsScoreBadge({ score }: { score: number | null | undefined }) {
  if (typeof score !== "number" || !Number.isFinite(score)) return null;
  const formattedScore = formatPoints(score);
  return (
    <span
      aria-label={`OpenNews 分数 ${formattedScore}`}
      className="news-provider-score"
      data-band={score > 70 ? "high" : "base"}
    >
      OpenNews
      <b>{formattedScore}</b>
    </span>
  );
}

export function NewsGroundedAssets({
  assets,
  hits,
}: {
  assets: readonly string[] | null | undefined;
  hits?: readonly string[] | null;
}) {
  const symbols = (assets ?? []).map((symbol) => symbol.trim()).filter(Boolean);
  const hitSet = new Set((hits ?? []).map((symbol) => symbol.trim().toUpperCase()));
  return (
    <span className="news-grounded-assets">
      <span>落地资产</span>
      {symbols.length ? (
        <ul>
          {symbols.map((symbol) => (
            <li
              data-watch={hitSet.has(symbol.toUpperCase()) ? "hit" : undefined}
              key={symbol}
              title={hitSet.has(symbol.toUpperCase()) ? "关注名单命中" : undefined}
            >
              {symbol}
            </li>
          ))}
        </ul>
      ) : (
        <span className="news-grounded-assets-empty">未落地</span>
      )}
    </span>
  );
}

export function NewsTriageStrip({ triage }: { triage: NewsTriageSummary | null }) {
  if (!triage) return null;
  const parts = [
    triage.direction ? directionLabel(triage.direction) : null,
    magnitudeLabel(triage.magnitude),
    triage.event_type ?? null,
  ].filter((part): part is string => Boolean(part));
  return (
    <span className="news-event-triage" data-direction={triage.direction ?? "none"}>
      {parts.length ? <b>{parts.join(" · ")}</b> : null}
      {triage.headline_zh ? <span>{triage.headline_zh}</span> : null}
      {triage.override_rule ? <small>规则 {triage.override_rule}</small> : null}
      {triage.throttled_by ? <small>节流 {triage.throttled_by}</small> : null}
    </span>
  );
}

export function NewsDeliveryState({ delivery }: { delivery: NewsDeliverySummary | null }) {
  if (!delivery) return null;
  return (
    <span
      aria-label={`推送状态 ${deliveryStateLabel(delivery.state)}`}
      className="news-event-delivery"
      data-state={delivery.state}
    >
      {deliveryStateLabel(delivery.state)}
      {delivery.settled_at_ms != null ? (
        <time dateTime={new Date(delivery.settled_at_ms).toISOString()}>
          {displayTime(delivery.settled_at_ms)}
        </time>
      ) : null}
    </span>
  );
}

function validContextLine(value: string | null | undefined, title: string): string | null {
  const line = value?.trim() ?? "";
  if (!line || line === title.trim() || line.length < 4) return null;
  return line;
}
