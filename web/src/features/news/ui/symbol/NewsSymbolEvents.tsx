import { newsEventPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import * as PageState from "@shared/ui/PageState";
import { SourceLine } from "@shared/ui/SourceLine";
import { Link } from "react-router-dom";

import type { NewsFeedEvent } from "../../api/newsQueries";
import { clockTime, displayTime, formatCount } from "../../model/newsLabels";
import {
  matchesLane,
  SYMBOL_LANES,
  symbolLaneLabel,
  type NewsSymbolLane,
} from "../../model/symbolLanes";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsKindBadge } from "../chrome/NewsKindBadge";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsReactionValue } from "../chrome/NewsQuoteValue";

/**
 * Every persisted Event kind for this name on one clock.
 *
 * The two lanes are mixed on purpose — a listing headline and an open-interest frame minutes apart is the
 * whole reason a per-token page exists, and the console had no surface where they appeared together.
 *
 * The lane tabs filter the loaded window in the browser, unlike the OI monitor's tabs, and the counts beside
 * them count exactly the rows they filter. That is the honest version of a client-side split: the feed has
 * no `lane` parameter, so a server count would describe a window this table is not showing. Both numbers
 * describe the same loaded set, and the source line says which window that is.
 */
export function NewsSymbolEvents({
  error,
  hasMore,
  lane,
  loading,
  loadingMore,
  onLaneChange,
  onLoadMore,
  onRetry,
  rows,
}: {
  error: unknown;
  hasMore: boolean;
  lane: NewsSymbolLane;
  loading: boolean;
  loadingMore: boolean;
  onLaneChange: (lane: NewsSymbolLane) => void;
  onLoadMore: () => void;
  onRetry: () => void;
  rows: readonly NewsFeedEvent[];
}) {
  const counts = Object.fromEntries(
    SYMBOL_LANES.map((value) => [value, rows.filter((event) => matchesLane(event, value)).length]),
  ) as Record<NewsSymbolLane, number>;
  const shown = rows.filter((event) => matchesLane(event, lane));

  return (
    <Card flush hint="全部事件类型共用一条时间轴" title="这个代币经历的事件">
      <div aria-label="按事件类型筛选" className="news-symbol-tabs" role="tablist">
        {SYMBOL_LANES.map((value) => (
          <button
            aria-selected={lane === value}
            className="news-symbol-tab"
            data-active={lane === value || undefined}
            key={value}
            onClick={() => onLaneChange(value)}
            role="tab"
            type="button"
          >
            {symbolLaneLabel(value)}
            <span aria-hidden className="news-symbol-tab-count">
              {formatCount(counts[value])}
            </span>
          </button>
        ))}
      </div>

      {error ? <PageState.Error error={error} onRetry={onRetry} /> : null}
      {!error && loading && rows.length === 0 ? (
        <PageState.Loading label="正在读取这个代币的事件" layout="panel" rows={4} />
      ) : null}
      {!error && !loading && rows.length === 0 ? (
        <EmptyNote>这个窗口里没有关于这个代币的事件。</EmptyNote>
      ) : null}
      {!error && rows.length > 0 && shown.length === 0 ? (
        <EmptyNote>已加载的事件里没有这个类型的。</EmptyNote>
      ) : null}

      {!error && shown.length > 0 ? (
        <div className="news-symbol-table">
          <div aria-hidden className="news-symbol-head">
            <span>TIME</span>
            <span>类型</span>
            <span>EVENT</span>
            <span>去向</span>
            <span className="news-symbol-num">1H / 4H</span>
          </div>
          {shown.map((event) => (
            <EventRow event={event} key={event.event_id} />
          ))}
        </div>
      ) : null}

      {!error && hasMore ? (
        <div className="news-symbol-more">
          <ActionButton disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? "正在加载" : "加载更早的事件"}
          </ActionButton>
          <small>已加载 {formatCount(rows.length)} 条</small>
        </div>
      ) : null}

      <SourceLine
        note="类型页签在已加载的这批里筛，计数与它筛的是同一批——feed 没有 lane 参数，服务端计数会描述另一个窗口"
        path="GET /api/news/feed?symbol={base}&hours=24"
      />
    </Card>
  );
}

function EventRow({ event }: { event: NewsFeedEvent }) {
  const triage = event.triage;
  const headline = triage?.headline_zh?.trim() || event.leader_title;
  return (
    <article className="news-symbol-row">
      <time
        className="news-symbol-time"
        dateTime={new Date(event.opened_at_ms).toISOString()}
        title={displayTime(event.opened_at_ms)}
      >
        {clockTime(event.opened_at_ms)}
      </time>
      <NewsKindBadge kind={event.event_kind} />
      <span className="news-symbol-headline">
        <Link to={newsEventPath(event.event_id)}>{headline}</Link>
        {triage ? <NewsDirectionChip triage={triage} withStrength={false} /> : null}
      </span>
      <span className="news-symbol-outcome">
        <NewsOutcomeBadge outcome={event.outcome} variant="text" />
      </span>
      <span className="news-symbol-num news-symbol-reaction">
        <NewsReactionValue horizon="1h" reaction={event.reaction} />
        <NewsReactionValue horizon="4h" reaction={event.reaction} />
      </span>
    </article>
  );
}
