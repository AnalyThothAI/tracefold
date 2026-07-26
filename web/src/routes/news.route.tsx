import { NewsPage } from "@features/news";
import { useParams } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { token } = useShellRouteContext();
  const { storyId } = useParams();
  return <NewsPage storyId={storyId ?? null} token={token} />;
}
