import { newsEventPath } from "@shared/routing/paths";
import type { MouseEvent } from "react";
import { Link } from "react-router-dom";

import type { NewsFeedEvent, NewsQuote } from "../../api/newsQueries";
import {
  absoluteTime,
  clockTime,
  directionTone,
  displayAssetRefs,
  relativeTime,
} from "../../model/newsLabels";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsKindBadge } from "../chrome/NewsKindBadge";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";

import "./newsEventRow.css";

/** Three chips fit a meta line; the rest are counted and listed in full one click away. */
const ROW_ASSET_CHIPS = 3;

/**
 * One Event in the feed: when · what · one outcome, tiered by that outcome.
 *
 * The row is `54 / 1fr / 150` with a 3px left rail carrying the *direction* — the pipeline's own state lives
 * in the right column as a word, because a coloured pill on every row draws a vertical band the reader stops
 * seeing. A held Event steps back rather than disappearing: smaller headline, secondary ink, grey state word,
 * and the server's `reason_zh` under it.
 *
 * The whole row opens the Event through a stretched headline link — one accessible name, no click handler on
 * a non-interactive element. `onOpen` intercepts a plain left click at desktop width so the Event opens in the
 * drawer beside the list instead of replacing it; a modified click, a middle click and every assistive path
 * still follow the real href.
 *
 * The approved list has no inline reaction or expansion controls. The headline link is the one way into the
 * Event, and the filter/search state follows it into the detail surface.
 */
export function NewsEventRow({
  event,
  fresh = false,
  onOpen,
  quotes,
  searchState,
}: {
  event: NewsFeedEvent;
  fresh?: boolean;
  onOpen?: (eventId: string, trigger: HTMLAnchorElement) => void;
  quotes?: Record<string, NewsQuote>;
  searchState?: string;
}) {
  const triage = event.triage;
  const headline = triage?.headline_zh?.trim() || event.title_zh?.trim() || event.leader_title;
  const showOriginal = headline !== event.leader_title;
  const assets = displayAssetRefs(event.grounded_assets ?? [], event.assets);
  const sentAt = event.delivery?.state === "sent" ? event.delivery.settled_at_ms : null;
  const openState = searchState == null ? undefined : { feedSearch: searchState };
  const onHeadlineClick = (clickEvent: MouseEvent<HTMLAnchorElement>) => {
    if (!onOpen) return;
    if (
      clickEvent.metaKey ||
      clickEvent.ctrlKey ||
      clickEvent.shiftKey ||
      clickEvent.button !== 0
    ) {
      return;
    }
    clickEvent.preventDefault();
    onOpen(event.event_id, clickEvent.currentTarget);
  };
  return (
    <article
      className="news-event-row"
      /* The 3px rail is the market call, not the pipeline state — the right column already owns that. */
      data-direction={directionTone(triage?.direction)}
      data-event-id={event.event_id}
      data-fresh={fresh || undefined}
      data-outcome={event.outcome.kind}
      data-outcome-group={event.outcome.group}
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
          <Link onClick={onHeadlineClick} state={openState} to={newsEventPath(event.event_id)}>
            {headline}
          </Link>
        </h2>
        {showOriginal ? <p className="news-event-original">{event.leader_title}</p> : null}
        <p className="news-event-meta">
          <span className="news-event-origin">{event.reporting_origin || "未知来源"}</span>
          {triage ? (
            <>
              <span aria-hidden className="news-event-divider">
                ·
              </span>
              <NewsDirectionChip triage={triage} withStrength={false} />
            </>
          ) : null}
          <span aria-hidden className="news-event-divider">
            ·
          </span>
          <NewsKindBadge kind={event.event_kind} />
          {assets.length ? (
            <>
              <span aria-hidden className="news-event-divider">
                ·
              </span>
              <NewsAssetChips assets={assets} max={ROW_ASSET_CHIPS} quotes={quotes} withPrice />
            </>
          ) : null}
        </p>
      </div>

      {/* Badge and reason are siblings rather than one column, so the grid can put the conclusion beside the
          clock on a phone card and above the reason on a desktop row without the DOM changing shape. */}
      <span className="news-event-badge">
        <NewsOutcomeBadge outcome={event.outcome} variant="text" />
      </span>
      {/* One line under the badge, never two: a sent row wants the time it went out, everything else wants
          the server's reason. */}
      {event.outcome.reason_zh ? (
        <span className="news-event-reason">{event.outcome.reason_zh}</span>
      ) : sentAt ? (
        <span className="news-event-reason">推送于 {clockTime(sentAt)}</span>
      ) : null}
    </article>
  );
}
