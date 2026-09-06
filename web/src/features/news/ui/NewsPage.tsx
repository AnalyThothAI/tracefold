import { NewsEventDetailPage } from "./detail/NewsEventDetailPage";
import { NewsFeedPage } from "./feed/NewsFeedPage";
import { NewsMarketPage } from "./market/NewsMarketPage";
import { NewsStatusPage } from "./status/NewsStatusPage";
import { NewsSymbolPage } from "./symbol/NewsSymbolPage";
import { NewsWalletsPage } from "./wallets/NewsWalletsPage";

type NewsPageProps = { token: string } & (
  | { view: "feed" | "status" | "market" | "wallets" }
  | { eventId: string; view: "event" }
  | { base: string; view: "symbol" }
);

/** The News route's six surfaces. The route module picks the view; each surface owns its own data. */
export function NewsPage(props: NewsPageProps) {
  if (props.view === "status") return <NewsStatusPage token={props.token} />;
  if (props.view === "market") return <NewsMarketPage token={props.token} />;
  if (props.view === "wallets") return <NewsWalletsPage token={props.token} />;
  if (props.view === "symbol") return <NewsSymbolPage base={props.base} token={props.token} />;
  if (props.view === "event")
    return <NewsEventDetailPage eventId={props.eventId} token={props.token} />;
  return <NewsFeedPage token={props.token} />;
}
