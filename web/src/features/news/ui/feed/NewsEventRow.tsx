import { newsEventPath } from "@shared/routing/paths";
import { FactGrid } from "@shared/ui/FactGrid";
import { Check, ChevronRight } from "lucide-react";
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
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";

import "./newsEventRow.css";

/** Three chips fit a meta line; the rest are counted and listed in full one keystroke away. */
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
 * The two controls (expand, select) are desktop-only by design. #87 removed the row's buttons because a
 * hover-only target means nothing under a thumb — these come back on the pointer widths where hover exists
 * and stay out of the phone card entirely.
 */
export function NewsEventRow({
  cursor = false,
  event,
  expanded = false,
  fresh = false,
  onExpand,
  onOpen,
  onSelect,
  quotes,
  searchState,
  selectable = false,
  selected = false,
}: {
  cursor?: boolean;
  event: NewsFeedEvent;
  expanded?: boolean;
  fresh?: boolean;
  onExpand?: (eventId: string) => void;
  onOpen?: (eventId: string) => void;
  onSelect?: (eventId: string, shiftKey: boolean) => void;
  quotes?: Record<string, NewsQuote>;
  searchState?: string;
  selectable?: boolean;
  selected?: boolean;
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
    onOpen(event.event_id);
  };
  return (
    <article
      className="news-event-row"
      data-cursor={cursor || undefined}
      /* The 3px rail is the market call, not the pipeline state — the right column already owns that. */
      data-direction={directionTone(triage?.direction)}
      data-event-id={event.event_id}
      data-expanded={expanded || undefined}
      data-fresh={fresh || undefined}
      data-outcome={event.outcome.kind}
      data-outcome-group={event.outcome.group}
      data-priority={event.priority}
      data-selectable={selectable || undefined}
      data-selected={selected || undefined}
      tabIndex={-1}
    >
      {selectable ? (
        <button
          aria-label={selected ? `取消选择 ${headline}` : `选择 ${headline}`}
          aria-pressed={selected}
          className="news-event-select"
          onClick={(clickEvent) => onSelect?.(event.event_id, clickEvent.shiftKey)}
          type="button"
        >
          <Check aria-hidden />
        </button>
      ) : null}

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
          {triage?.event_type_zh ? (
            <>
              <span aria-hidden className="news-event-divider">
                ·
              </span>
              <span className="news-event-kind">{triage.event_type_zh}</span>
            </>
          ) : null}
          {assets.length ? (
            <>
              <span aria-hidden className="news-event-divider">
                ·
              </span>
              <NewsAssetChips assets={assets} max={ROW_ASSET_CHIPS} quotes={quotes} />
            </>
          ) : null}
          {onExpand ? (
            <button
              aria-expanded={expanded}
              aria-label={expanded ? `收起判定 ${headline}` : `展开判定 ${headline}`}
              className="news-event-expand"
              onClick={() => onExpand(event.event_id)}
              type="button"
            >
              <ChevronRight aria-hidden />
            </button>
          ) : null}
        </p>
        {expanded && triage ? <RowVerdict event={event} triage={triage} /> : null}
      </div>

      {/* Badge and reason are siblings rather than one column, so the grid can put the conclusion beside the
          clock on a phone card and above the reason on a desktop row without the DOM changing shape. */}
      <span className="news-event-badge">
        <NewsOutcomeBadge
          outcome={event.outcome}
          /*
           * One filled thing per screenful, and it marks the loudest Events: a high-priority *push*. Not
           * merely "not held" — that would give the capsule to an Event still sitting in the delivery queue,
           * before anything has reached a reader.
           */
          variant={event.priority === "high" && event.outcome.group === "pushed" ? "chip" : "text"}
        />
      </span>
      {/* One line under the badge, never two: a sent row wants the time it went out, everything else wants
          the server's reason. */}
      {sentAt ? (
        <span className="news-event-reason">推送于 {clockTime(sentAt)}</span>
      ) : event.outcome.reason_zh ? (
        <span className="news-event-reason">{event.outcome.reason_zh}</span>
      ) : null}
    </article>
  );
}

/**
 * The judgment, in place (design proposal ②).
 *
 * Reading why an Event was held used to mean leaving the list and losing your position for one sentence. This
 * is that sentence plus the verdict grid — server copy only, no rule keys, admissions or decisions, which
 * stay behind 技术详情 on the Event's own page where an operator goes to audit rather than to scan.
 */
function RowVerdict({
  event,
  triage,
}: {
  event: NewsFeedEvent;
  triage: NonNullable<NewsFeedEvent["triage"]>;
}) {
  const assets = displayAssetRefs(event.grounded_assets ?? [], event.assets);
  // A thin verdict — a duplicate, a recovery replay — often has no `why_zh`. The outcome's own sentence is
  // then the only prose there is, and an expansion that opens onto a lone fact cell explains nothing.
  const why = triage.why_zh?.trim() || event.outcome.reason_zh;
  return (
    <div className="news-event-verdict">
      {why ? <p className="news-event-why">{why}</p> : null}
      <FactGrid
        facts={[
          { label: "影响", value: triage.magnitude_zh },
          { label: "范围", value: triage.scope_zh },
          {
            label: "把握",
            value: triage.confidence == null ? "" : `${Math.round(triage.confidence * 100)}%`,
          },
          { label: "新颖度", value: triage.novelty_zh },
          {
            label: "可操作",
            value: triage.actionable == null ? "" : triage.actionable ? "是" : "否",
          },
          { label: "受众", value: triage.audience_zh },
        ]}
        label="判定明细"
      />
      {assets.length > ROW_ASSET_CHIPS ? (
        <NewsAssetChips assets={assets} label="全部关联资产" />
      ) : null}
    </div>
  );
}
