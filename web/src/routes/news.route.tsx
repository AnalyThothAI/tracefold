import { NewsPage } from "@features/news";
import { useLocation, useParams } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { token } = useShellRouteContext();
  const { storyId } = useParams();
  const location = useLocation();
  return (
    <NewsPage
      brief={location.pathname === "/news/brief"}
      storyId={storyId ?? null}
      token={token}
    />
  );
}
