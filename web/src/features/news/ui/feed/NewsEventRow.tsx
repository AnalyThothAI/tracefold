import { newsEventPath } from "@shared/routing/paths";
import { Copy, ExternalLink, Layers, Tag, Zap } from "lucide-react";
import { Link } from "react-router-dom";

import type { NewsFeedEvent } from "../../api/newsQueries";
import {
  absoluteTime,
  clockTime,
  displayAssets,
  labelCommand,
  relativeTime,
  validExternalUrl,
} from "../../model/newsLabels";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";

import "./newsEventRow.css";

/**
 * One Event in the feed: when · what (Chinese headline, original wire line) · one outcome. Model facts
 * (direction / magnitude / type) come pre-labelled from the server; nothing here maps a rule key to copy.
 *
 * A held Event does not get a badge — see `newsEventRow.css` for why. Its `outcome.reason_zh` still carries
 * the conclusion, and `data-outcome` / `data-outcome-group` remain on the row for styling and for tests.
 *
 * The whole row opens the Event, but the row itself is not a click target: the headline link stretches over
 * it. One link, one accessible name, and `Enter` works from the keyboard without the row pretending to be a
 * button that contains other buttons.
 */
export function NewsEventRow({
  cursor = false,
  event,
  onCopy,
  searchState,
}: {
  cursor?: boolean;
  event: NewsFeedEvent;
  onCopy?: (text: string, note: string) => void;
  searchState?: string;
}) {
  const triage = event.triage;
  const headline = triage?.headline_zh?.trim() || event.title_zh?.trim() || event.leader_title;
  const showOriginal = headline !== event.leader_title;
  const url = validExternalUrl(event.leader_url);
  const assets = displayAssets(event.grounded_assets ?? []);
  const held = event.outcome.group === "held";
  const to = newsEventPath(event.event_id);
  const openState = searchState == null ? undefined : { feedSearch: searchState };
  return (
    <article
      className="news-event-row"
      data-cursor={cursor || undefined}
      data-event-id={event.event_id}
      data-outcome={event.outcome.kind}
      data-outcome-group={event.outcome.group}
      data-priority={event.priority}
      tabIndex={-1}
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
          <Link state={openState} to={to}>
            {event.priority === "high" ? (
              <Zap aria-hidden className="news-event-priority-icon" data-title="高优先级" />
            ) : null}
            {headline}
          </Link>
        </h2>
        {showOriginal ? <p className="news-event-original">{event.leader_title}</p> : null}
        <p className="news-event-meta">
          <span className="news-event-origin">{event.reporting_origin || "未知来源"}</span>
          {event.member_count > 1 ? (
            <span className="news-event-members" title="归并的同类报道数">
              <Layers aria-hidden />
              {event.member_count} 条报道
            </span>
          ) : null}
          {triage ? (
            <>
              <span aria-hidden className="news-event-divider">
                |
              </span>
              <NewsDirectionChip triage={triage} />
            </>
          ) : null}
          {triage?.event_type_zh ? (
            <>
              <span aria-hidden className="news-event-divider">
                |
              </span>
              <span className="news-event-facts">{triage.event_type_zh}</span>
            </>
          ) : null}
          {assets.length ? (
            <span aria-label="关联资产" className="news-event-assets">
              {assets.map((symbol) => (
                <code key={symbol}>{symbol}</code>
              ))}
            </span>
          ) : null}
        </p>
      </div>

      <div className="news-event-outcome">
        {held ? null : (
          <NewsOutcomeBadge
            emphasis={event.priority === "high" && event.outcome.group === "pushed" ? "solid" : "soft"}
            outcome={event.outcome}
          />
        )}
        {event.outcome.reason_zh ? (
          <span className="news-event-reason">{event.outcome.reason_zh}</span>
        ) : null}
        {event.delivery?.state === "sent" && event.delivery.settled_at_ms ? (
          <span className="news-event-reason">
            推送于 {clockTime(event.delivery.settled_at_ms)}
          </span>
        ) : null}
        <span className="news-event-actions">
          {url ? (
            <a aria-label="打开原文" href={url} rel="noreferrer" target="_blank" title="原文">
              <ExternalLink aria-hidden />
            </a>
          ) : null}
          <button
            aria-label="复制标题"
            onClick={() => onCopy?.(headline, "已复制标题")}
            title="复制标题"
            type="button"
          >
            <Copy aria-hidden />
          </button>
          <button
            aria-label="复制标注命令"
            onClick={() => onCopy?.(labelCommand(event.event_id, "bad"), "已复制「判错了」标注命令")}
            title="复制「判错了」的 tracefold news label 命令"
            type="button"
          >
            <Tag aria-hidden />
            判错了
          </button>
        </span>
      </div>
    </article>
  );
}

