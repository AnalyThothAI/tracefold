import { newsEventPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { useNewsFeedWithToken } from "../../api/newsQueries";
import { parseFeedFilters } from "../../model/feedFilters";
import { formatCount } from "../../model/newsLabels";

/**
 * 上一条 / 下一条 over the feed the reader came from.
 *
 * The list is whatever `/news` was showing: the search string travels in the navigation state, and reading the
 * feed under those exact filters hits the query React Query already has, so no request is made for the sake of
 * the pager. A cold URL carries no such state — the pager renders nothing rather than inventing an order, and
 * a paged-in Event that is not on the first page simply has no neighbours here.
 */
export function NewsEventPager({
  eventId,
  feedSearch,
  token,
}: {
  eventId: string;
  feedSearch: string | null;
  token: string;
}) {
  const navigate = useNavigate();
  const filters = parseFeedFilters(new URLSearchParams(feedSearch ?? ""));
  const query = useNewsFeedWithToken(feedSearch == null ? "" : token, filters);
  const events = query.data?.events ?? [];
  const index = events.findIndex((event) => event.event_id === eventId);
  const previous = index > 0 ? events[index - 1] : null;
  const next = index >= 0 && index < events.length - 1 ? events[index + 1] : null;

  const go = (target: { event_id: string } | null) => {
    if (target) navigate(newsEventPath(target.event_id), { state: { feedSearch } });
  };
  if (index < 0) return null;
  return (
    <div className="news-detail-pager">
      <span className="news-detail-pager-position">
        {formatCount(index + 1)} / {formatCount(events.length)}
      </span>
      <ActionButton disabled={!previous} onClick={() => go(previous)} size="sm">
        <ChevronLeft aria-hidden />
        上一条
      </ActionButton>
      <ActionButton disabled={!next} onClick={() => go(next)} size="sm">
        下一条
        <ChevronRight aria-hidden />
      </ActionButton>
    </div>
  );
}
