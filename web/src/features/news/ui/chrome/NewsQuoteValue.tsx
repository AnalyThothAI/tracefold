import type { NewsEventReaction, NewsQuote, NewsReaction } from "../../api/newsQueries";
import {
  formatBps,
  formatChangePct,
  formatPrice,
  priceTone,
  quoteAgeLabel,
  quoteVenueLabel,
  reactionPlaceholder,
  reactionValue,
} from "../../model/newsPrice";

import "./newsQuote.css";

/**
 * The two market values, deliberately rendered as two different things (#88).
 *
 * `NewsQuoteValue` is *now*: a current price on a named contract, with its age and its price kind. It moves
 * every few seconds and says how old it is; when it goes stale it stays on screen, marked, because a provider
 * outage that blanks a price looks exactly like a market that moved.
 *
 * `NewsReactionValue` is *then*: the fixed return between an Event's anchor and a horizon. It never changes
 * once complete. A horizon that has not matured says 未到期 — it is never drawn as 0.00%.
 */
export function NewsQuoteValue({ quote }: { quote: NewsQuote | undefined }) {
  if (!quote || quote.state === "unlisted") {
    return (
      <span
        className="news-quote"
        data-state="unlisted"
        title="该符号没有可交易合约，本地没有行情源"
      >
        {quote?.state_zh ?? "无可交易合约"}
      </span>
    );
  }
  if (quote.state === "unavailable" || !quote.price) {
    return (
      <span className="news-quote" data-state="unavailable" title={quoteVenueLabel(quote)}>
        {quote.state_zh || "暂无报价"}
      </span>
    );
  }
  const tone = priceTone(quote.change_pct);
  return (
    <span
      className="news-quote"
      data-state={quote.state}
      data-tone={tone}
      title={`${quoteVenueLabel(quote)} · ${quote.price_kind_zh} · ${quoteAgeLabel(quote)}${
        quote.change_basis_zh ? ` · ${quote.change_basis_zh}变动` : ""
      }`}
    >
      <span className="news-quote-price">{formatPrice(quote.price)}</span>
      {quote.change_pct == null ? null : (
        <span className="news-quote-change">{formatChangePct(quote.change_pct)}</span>
      )}
      {quote.state === "stale" ? <span className="news-quote-stale">陈旧</span> : null}
    </span>
  );
}

export function NewsReactionValue({
  horizon,
  reaction,
}: {
  horizon: "1h" | "4h";
  reaction: NewsEventReaction | NewsReaction | null | undefined;
}) {
  const value = reactionValue(reaction, horizon);
  const label = horizon === "1h" ? "1H" : "4H";
  if (value == null) {
    return (
      <span className="news-reaction" data-state={reaction?.state ?? "pending"}>
        <small>{label}</small>
        <span className="news-reaction-empty">{reactionPlaceholder(reaction, horizon)}</span>
      </span>
    );
  }
  return (
    <span
      className="news-reaction"
      data-state={reaction?.state}
      data-tone={priceTone(value)}
      title={`事件后 ${label} 的实际涨跌，锚点是新闻发布时间`}
    >
      <small>{label}</small>
      <b>{formatBps(value)}</b>
    </span>
  );
}
