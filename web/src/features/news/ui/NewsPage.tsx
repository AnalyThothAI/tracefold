import { NewsEventDetailPage } from "./detail/NewsEventDetailPage";
import { NewsFeedPage } from "./feed/NewsFeedPage";
import { NewsOiPage } from "./oi/NewsOiPage";
import { NewsReviewPage } from "./review/NewsReviewPage";
import { NewsStatusPage } from "./status/NewsStatusPage";
import { NewsSymbolPage } from "./symbol/NewsSymbolPage";

type NewsPageProps = { token: string } & (
  | { view: "feed" | "status" | "review" | "oi" }
  | { eventId: string; view: "event" }
  | { base: string; view: "symbol" }
);

/** The News route's six surfaces. The route module picks the view; each surface owns its own data. */
export function NewsPage(props: NewsPageProps) {
  if (props.view === "status") return <NewsStatusPage token={props.token} />;
  if (props.view === "oi") return <NewsOiPage token={props.token} />;
  if (props.view === "review") return <NewsReviewPage token={props.token} />;
  if (props.view === "symbol") return <NewsSymbolPage base={props.base} token={props.token} />;
  if (props.view === "event")
    return <NewsEventDetailPage eventId={props.eventId} token={props.token} />;
  return <NewsFeedPage token={props.token} />;
}
