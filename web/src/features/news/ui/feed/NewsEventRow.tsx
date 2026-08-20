import { newsEventPath } from "@shared/routing/paths";
import { Layers, Zap } from "lucide-react";
import { Link } from "react-router-dom";

import type { NewsFeedEvent } from "../../api/newsQueries";
import { absoluteTime, clockTime, displayAssetRefs, relativeTime } from "../../model/newsLabels";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
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
 * The row carries no buttons of its own (#87). Copy-title, copy-label and open-original all lived here and
 * all led somewhere better one tap away, while costing every row a hover target that meant nothing on a
 * phone. The keyboard keeps its `X` shortcut through the feed cursor, so the fast path did not move.
 *
 * The whole row opens the Event, but the row itself is not a click target: the headline link stretches over
 * it. One link, one accessible name, and `Enter` works from the keyboard without the row pretending to be a
 * button that contains other buttons.
 */
export function NewsEventRow({
  cursor = false,
  event,
  searchState,
}: {
  cursor?: boolean;
  event: NewsFeedEvent;
  searchState?: string;
}) {
  const triage = event.triage;
  const headline = triage?.headline_zh?.trim() || event.title_zh?.trim() || event.leader_title;
  const showOriginal = headline !== event.leader_title;
  const assets = displayAssetRefs(event.grounded_assets ?? [], event.assets);
  const held = event.outcome.group === "held";
  const sentAt = event.delivery?.state === "sent" ? event.delivery.settled_at_ms : null;
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
          <NewsAssetChips assets={assets} />
        </p>
      </div>

      {/* Badge and reason are siblings rather than one column, so the grid can put the conclusion beside the
          clock on a phone card and above the reason on a desktop row without the DOM changing shape. */}
      <span className="news-event-badge">
        {held ? null : (
          <NewsOutcomeBadge
            emphasis={
              event.priority === "high" && event.outcome.group === "pushed" ? "solid" : "soft"
            }
            outcome={event.outcome}
          />
        )}
      </span>
      {/* One line under the badge, never two: a sent row wants the time it went out, everything else
          wants the server's reason. */}
      {sentAt ? (
        <span className="news-event-reason">推送于 {clockTime(sentAt)}</span>
      ) : event.outcome.reason_zh ? (
        <span className="news-event-reason">{event.outcome.reason_zh}</span>
      ) : null}
    </article>
  );
}
