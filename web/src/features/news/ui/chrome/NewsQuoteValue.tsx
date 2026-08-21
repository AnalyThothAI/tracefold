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
 * A quote is *now*: a current price on a named contract. It moves every few seconds, so it carries its venue,
 * its price kind and its age in a tooltip; when it goes stale it stays on screen, dimmed and marked, because a
 * provider outage that blanks a price looks exactly like a market that moved.
 *
 * A reaction is *then*: the fixed return between an Event's anchor and a horizon. It never changes once
 * complete. A horizon that has not matured says 未到期 — it is never drawn as 0.00%.
 *
 * Both use red-up / green-down, the same convention as the direction word, and are told apart from the
 * model's judgment by weight: the judgment is a word, the outcome is figures.
 */

/** The compact half of a quote: what it did, for a meta line where a full price would not fit. */
export function NewsQuoteChange({ quote }: { quote: NewsQuote | undefined }) {
  if (!quote || quote.state === "unlisted" || quote.state === "unavailable") return null;
  if (quote.change_pct == null) return null;
  return (
    <span
      className="news-quote-change"
      data-state={quote.state}
      data-tone={priceTone(quote.change_pct)}
      title={quoteTitle(quote)}
    >
      {formatChangePct(quote.change_pct)}
    </span>
  );
}

/** The price itself, for the detail page's quote table where the column has room for it. */
export function NewsQuotePrice({ quote }: { quote: NewsQuote | undefined }) {
  if (!quote || quote.state === "unlisted") {
    return (
      <span className="news-quote-state" title="该符号没有可交易合约，本地没有行情源">
        {quote?.state_zh ?? "无可交易合约"}
      </span>
    );
  }
  if (quote.state === "unavailable" || !quote.price) {
    return (
      <span className="news-quote-state" title={quoteVenueLabel(quote)}>
        {quote.state_zh || "暂无报价"}
      </span>
    );
  }
  return (
    <span className="news-quote-price" data-state={quote.state} title={quoteTitle(quote)}>
      {formatPrice(quote.price)}
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

function quoteTitle(quote: NewsQuote): string {
  return [
    quoteVenueLabel(quote),
    quote.price_kind_zh,
    quoteAgeLabel(quote),
    quote.change_basis_zh && quote.change_pct != null ? `${quote.change_basis_zh}变动` : "",
    quote.state === "stale" ? "报价陈旧" : "",
  ]
    .filter(Boolean)
    .join(" · ");
}
