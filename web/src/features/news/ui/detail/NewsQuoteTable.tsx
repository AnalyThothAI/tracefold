import type { NewsQuote } from "../../api/newsQueries";
import { NewsEmptyNote } from "../chrome/NewsChrome";
import { NewsQuoteChange, NewsQuotePrice } from "../chrome/NewsQuoteValue";

import "./newsQuoteTable.css";

/**
 * 当前报价 (#88): what these contracts are worth *now*, on named venues.
 *
 * Deliberately never merged with 事件后反应 below it. One is a moving current value and the other is a fixed
 * measurement anchored at this Event; one table would invite reading a rolling 24 h change as the market's
 * answer to this headline, which is the single wrong conclusion this whole plane exists to prevent.
 */
export function NewsQuoteTable({ quoteTime, quotes }: { quoteTime?: string; quotes: NewsQuote[] }) {
  if (!quotes.length) return <NewsEmptyNote>这条事件没有可以定价的标的。</NewsEmptyNote>;
  return (
    <div className="news-quote-table">
      <div className="news-quote-table-head">
        <span>ASSET · {quotes.length} 个标的</span>
        <span>现价</span>
        <span>24H</span>
      </div>
      {quotes.map((quote) => (
        <div className="news-quote-table-row" key={quote.requested_symbol}>
          <code>
            {quote.venue ? <span>{quote.venue}:</span> : null}
            <b>{quote.venue_symbol ?? quote.symbol}</b>
          </code>
          <NewsQuotePrice quote={quote} />
          <NewsQuoteChange quote={quote} />
        </div>
      ))}
      <p className="news-quote-table-note">
        读取于 {quoteTime ?? "刚刚"} 的现价，不是事件时点的回填收益。
      </p>
    </div>
  );
}
