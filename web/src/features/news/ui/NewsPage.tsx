import { NewsEventDetailPage } from "./detail/NewsEventDetailPage";
import { NewsFeedPage } from "./feed/NewsFeedPage";
import { NewsStatusPage } from "./status/NewsStatusPage";

type NewsPageProps =
  | { token: string; view: "feed" | "status" }
  | { eventId: string; token: string; view: "event" };

/** The News route's three surfaces. The route module picks the view; each surface owns its own data. */
export function NewsPage(props: NewsPageProps) {
  if (props.view === "status") return <NewsStatusPage token={props.token} />;
  if (props.view === "event")
    return <NewsEventDetailPage eventId={props.eventId} token={props.token} />;
  return <NewsFeedPage token={props.token} />;
}
