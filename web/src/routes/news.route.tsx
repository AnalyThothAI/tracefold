import { NewsPage } from "@features/news";
import { useLocation, useParams } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { token } = useShellRouteContext();
  const { eventId } = useParams();
  const location = useLocation();
  if (eventId) return <NewsPage eventId={eventId} token={token} view="event" />;
  if (location.pathname === "/news/status") return <NewsPage token={token} view="status" />;
  return <NewsPage token={token} view="feed" />;
}
