import { NewsPage } from "@features/news";
import { useLocation, useParams } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { token } = useShellRouteContext();
  const { base, eventId } = useParams();
  const location = useLocation();
  if (eventId) return <NewsPage eventId={eventId} token={token} view="event" />;
  if (base) return <NewsPage base={base} token={token} view="symbol" />;
  if (location.pathname === "/news/status") return <NewsPage token={token} view="status" />;
  if (location.pathname === "/news/oi") return <NewsPage token={token} view="oi" />;
  if (location.pathname === "/news/review") return <NewsPage token={token} view="review" />;
  return <NewsPage token={token} view="feed" />;
}
