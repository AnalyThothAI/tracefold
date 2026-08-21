import { NewsEventDetailPage } from "./detail/NewsEventDetailPage";
import { NewsFeedPage } from "./feed/NewsFeedPage";
import { NewsReviewPage } from "./review/NewsReviewPage";
import { NewsStatusPage } from "./status/NewsStatusPage";

type NewsPageProps = {
  /** The shell's clipboard affordance: every route confirms a copy through the one console toast. */
  copy: (text: string, note: string) => void;
  token: string;
} & ({ view: "feed" | "status" | "review" } | { eventId: string; view: "event" });

/** The News route's four surfaces. The route module picks the view; each surface owns its own data. */
export function NewsPage(props: NewsPageProps) {
  if (props.view === "status") return <NewsStatusPage token={props.token} />;
  if (props.view === "review") return <NewsReviewPage copy={props.copy} token={props.token} />;
  if (props.view === "event")
    return <NewsEventDetailPage copy={props.copy} eventId={props.eventId} token={props.token} />;
  return <NewsFeedPage copy={props.copy} token={props.token} />;
}
