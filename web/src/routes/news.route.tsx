import { NewsPage } from "@features/news";
import { useLocation, useParams } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { copy, token } = useShellRouteContext();
  const { eventId } = useParams();
  const location = useLocation();
  if (eventId) return <NewsPage copy={copy} eventId={eventId} token={token} view="event" />;
  if (location.pathname === "/news/status")
    return <NewsPage copy={copy} token={token} view="status" />;
  if (location.pathname === "/news/review")
    return <NewsPage copy={copy} token={token} view="review" />;
  return <NewsPage copy={copy} token={token} view="feed" />;
}
