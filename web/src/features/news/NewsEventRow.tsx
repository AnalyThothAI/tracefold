import { newsEventPath } from "@shared/routing/paths";
import { ExternalLink, Layers, Zap } from "lucide-react";
import { Link } from "react-router-dom";

import "./newsEventRow.css";
import { NewsOutcomeBadge } from "./NewsOutcomeBadge";
import {
  absoluteTime,
  clockTime,
  displayAssets,
  relativeTime,
  validExternalUrl,
} from "./newsLabels";
import type { NewsFeedEvent } from "./useNewsPage";

/**
 * One Event in the feed: when · what (Chinese headline, original wire line) · one outcome. Model facts
 * (direction / magnitude / type) come pre-labelled from the server; nothing here maps a rule key to copy.
 */
export function NewsEventRow({ event }: { event: NewsFeedEvent }) {
  const triage = event.triage;
  const headline = triage?.headline_zh?.trim() || event.title_zh?.trim() || event.leader_title;
  const showOriginal = headline !== event.leader_title;
  const url = validExternalUrl(event.leader_url);
  const assets = displayAssets(event.grounded_assets ?? []);
  const facts = triage
    ? [triage.direction_zh, triage.magnitude_zh, triage.event_type_zh].filter(Boolean)
    : [];
  const held = event.outcome.group === "held";
  return (
    <article
      className="news-event-row"
      data-outcome={event.outcome.kind}
      data-outcome-group={event.outcome.group}
      data-priority={event.priority}
    >
      <time
        className="news-event-time"
        dateTime={new Date(event.opened_at_ms).toISOString()}
        title={`${absoluteTime(event.opened_at_ms)} · ${relativeTime(event.opened_at_ms)}`}
      >
        {clockTime(event.opened_at_ms)}
      </time>

      <div className="news-event-main">
        <h2 className="news-event-headline">
          <Link to={newsEventPath(event.event_id)}>
            {event.priority === "high" ? (
              <Zap aria-hidden className="news-event-priority-icon" data-title="高优先级" />
            ) : null}
            {headline}
          </Link>
        </h2>
        {showOriginal ? <p className="news-event-original">{event.leader_title}</p> : null}
        <p className="news-event-meta">
          <span>{event.reporting_origin || "未知来源"}</span>
          {event.member_count > 1 ? (
            <span className="news-event-members" title="归并的同类报道数">
              <Layers aria-hidden />
              {event.member_count} 条报道
            </span>
          ) : null}
          {facts.length ? <span className="news-event-facts">{facts.join(" · ")}</span> : null}
          {assets.length ? (
            <span aria-label="关联资产" className="news-event-assets">
              {assets.map((symbol) => (
                <code key={symbol}>{symbol}</code>
              ))}
            </span>
          ) : null}
          {url ? (
            <a
              aria-label="打开原文"
              className="news-event-link"
              href={url}
              rel="noreferrer"
              target="_blank"
            >
              原文
              <ExternalLink aria-hidden />
            </a>
          ) : null}
        </p>
      </div>

      <div className="news-event-outcome">
        <NewsOutcomeBadge outcome={event.outcome} />
        {held && event.outcome.reason_zh ? (
          <span className="news-event-reason">{event.outcome.reason_zh}</span>
        ) : null}
        {event.delivery?.state === "sent" && event.delivery.settled_at_ms ? (
          <span className="news-event-reason">
            推送于 {clockTime(event.delivery.settled_at_ms)}
          </span>
        ) : null}
      </div>
    </article>
  );
}
