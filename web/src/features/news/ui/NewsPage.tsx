import { NewsEventDetailPage } from "./detail/NewsEventDetailPage";
import { NewsFeedPage } from "./feed/NewsFeedPage";
import { NewsLeveragePage } from "./leverage/NewsLeveragePage";
import { NewsOiPage } from "./oi/NewsOiPage";
import { NewsStatusPage } from "./status/NewsStatusPage";
import { NewsSymbolPage } from "./symbol/NewsSymbolPage";

type NewsPageProps = { token: string } & (
  | { view: "alpha" | "feed" | "status" | "oi" }
  | { eventId: string; view: "event" }
  | { base: string; view: "symbol" }
);

/** The News route's six surfaces. The route module picks the view; each surface owns its own data. */
export function NewsPage(props: NewsPageProps) {
  if (props.view === "status") return <NewsStatusPage token={props.token} />;
  if (props.view === "oi") return <NewsOiPage token={props.token} />;
  if (props.view === "alpha") return <NewsLeveragePage token={props.token} />;
  if (props.view === "symbol") return <NewsSymbolPage base={props.base} token={props.token} />;
  if (props.view === "event")
    return <NewsEventDetailPage eventId={props.eventId} token={props.token} />;
  return <NewsFeedPage token={props.token} />;
}
