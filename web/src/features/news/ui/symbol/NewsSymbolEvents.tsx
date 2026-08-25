import { newsEventPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import * as PageState from "@shared/ui/PageState";
import { Link } from "react-router-dom";

import type { NewsFeedEvent } from "../../api/newsQueries";
import { clockTime, displayTime, formatCount } from "../../model/newsLabels";
import {
  isOiFrame,
  matchesLane,
  SYMBOL_LANE_LABELS,
  SYMBOL_LANES,
  type NewsSymbolLane,
} from "../../model/symbolLanes";
import { NewsEmptyNote } from "../chrome/NewsChrome";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsReactionValue } from "../chrome/NewsQuoteValue";
import { NewsSourceLine } from "../chrome/NewsSourceLine";

/**
 * Everything that happened to this name, news and OI frames on one clock.
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
  const counts: Record<NewsSymbolLane, number> = {
    all: rows.length,
    news: rows.filter((event) => !isOiFrame(event)).length,
    oi: rows.filter(isOiFrame).length,
    pushed: rows.filter((event) => event.outcome.group === "pushed").length,
  };
  const shown = rows.filter((event) => matchesLane(event, lane));

  return (
    <Card flush hint="新闻与 OI 帧同一条时间轴" title="这个代币经历的事件">
      <div aria-label="按通道筛选" className="news-symbol-tabs" role="tablist">
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
            {SYMBOL_LANE_LABELS[value]}
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
        <NewsEmptyNote>这个窗口里没有关于这个代币的事件。</NewsEmptyNote>
      ) : null}
      {!error && rows.length > 0 && shown.length === 0 ? (
        <NewsEmptyNote>已加载的事件里没有这个通道的。</NewsEmptyNote>
      ) : null}

      {!error && shown.length > 0 ? (
        <div className="news-symbol-table">
          <div aria-hidden className="news-symbol-head">
            <span>TIME</span>
            <span>通道</span>
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

      <NewsSourceLine
        note="通道页签在已加载的这批里筛，计数与它筛的是同一批——feed 没有 lane 参数，服务端计数会描述另一个窗口"
        path="GET /api/news/feed?symbol={base}&hours=24"
      />
    </Card>
  );
}

function EventRow({ event }: { event: NewsFeedEvent }) {
  const triage = event.triage;
  const headline = triage?.headline_zh?.trim() || event.title_zh?.trim() || event.leader_title;
  return (
    <article className="news-symbol-row">
      <time
        className="news-symbol-time"
        dateTime={new Date(event.opened_at_ms).toISOString()}
        title={displayTime(event.opened_at_ms)}
      >
        {clockTime(event.opened_at_ms)}
      </time>
      <span className="news-symbol-lane">{isOiFrame(event) ? "OI 帧" : "新闻"}</span>
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
