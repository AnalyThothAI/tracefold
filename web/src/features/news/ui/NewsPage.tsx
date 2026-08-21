import { NewsEventDetailPage } from "./detail/NewsEventDetailPage";
import { NewsFeedPage } from "./feed/NewsFeedPage";
import { NewsReviewPage } from "./review/NewsReviewPage";
import { NewsStatusPage } from "./status/NewsStatusPage";

type NewsPageProps = {
  token: string;
} & ({ view: "feed" | "status" | "review" } | { eventId: string; view: "event" });

/** The News route's four surfaces. The route module picks the view; each surface owns its own data. */
export function NewsPage(props: NewsPageProps) {
  if (props.view === "status") return <NewsStatusPage token={props.token} />;
  if (props.view === "review") return <NewsReviewPage token={props.token} />;
  if (props.view === "event")
    return <NewsEventDetailPage eventId={props.eventId} token={props.token} />;
  return <NewsFeedPage token={props.token} />;
}
