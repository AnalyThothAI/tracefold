import { NewsPage } from "@features/news";
import { useLocation, useParams } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { token } = useShellRouteContext();
  const { storyId } = useParams();
  const location = useLocation();
  if (storyId) return <NewsPage storyId={storyId} token={token} view="story" />;
  if (location.pathname === "/news/brief") return <NewsPage token={token} view="brief" />;
  if (location.pathname === "/news/status") return <NewsPage token={token} view="status" />;
  if (location.pathname === "/news/sources") return <NewsPage token={token} view="sources" />;
  return <NewsPage token={token} view="feed" />;
}
