import { Card } from "@shared/ui/Card";

import type { NewsQuote, NewsSymbol } from "../../api/newsQueries";
import { NewsQuotePrice } from "../chrome/NewsQuoteValue";
import { NewsSourceLine } from "../chrome/NewsSourceLine";

/**
 * What this name *is*, before anything about what happened to it.
 *
 * Three separate facts, kept separate on purpose. The contracts come from the instrument universe (#75/#89)
 * and say which venues list the name. `tradeable` is the #91 distinction: `us.listed` proves a ticker
 * exists, it does not prove anyone can trade it, so a base whose only row is the reference tier renders its
 * contract *and* says the lane cannot act on it. The quote is a third poll (#88) and is never a zero — an
 * unquoted name says so in words.
 *
 * A base the universe has never listed is not an error page. Every asset chip on the console links here,
 * including the struck-through ones, and "the provider tagged a name nothing lists" is the answer the reader
 * came for.
 */
export function NewsSymbolIdentity({
  quote,
  symbol,
}: {
  quote: NewsQuote | undefined;
  symbol: NewsSymbol | undefined;
}) {
  const contracts = symbol?.contracts ?? [];
  const tradeable = contracts.filter((contract) => !contract.reference_only);
  const aliases = symbol?.normalization?.aliases ?? [];
  return (
    <Card flush title="标的身份" titleStyle="eyebrow">
      <div className="news-symbol-identity">
        <div className="news-symbol-identity-main">
          <p className="news-symbol-classes">
            {symbol == null ? (
              <span className="news-symbol-muted">正在读取标的身份…</span>
            ) : symbol.known ? (
              <>
                {[...new Set(contracts.map((contract) => contract.instrument_class))].map(
                  (instrumentClass) => (
                    <code className="news-symbol-class" key={instrumentClass}>
                      {instrumentClass}
                    </code>
                  ),
                )}
                <span
                  className="news-symbol-tradeable"
                  data-tradeable={symbol.tradeable || undefined}
                >
                  {symbol.tradeable ? "已落标的表" : "仅参考行情，无可交易合约"}
                </span>
              </>
            ) : (
              <span className="news-symbol-unknown">
                我们轮询的场所都没有这个名字——供应商标了它，标的表里查不到
              </span>
            )}
          </p>
          {aliases.length > 1 ? (
            <p className="news-symbol-aliases">
              归一自{" "}
              {aliases.map((alias) => (
                <code key={alias}>{alias}</code>
              ))}
            </p>
          ) : null}
        </div>

        <div className="news-symbol-quote">
          <small>MARK · {quote?.state_zh || (quote ? quote.state : "—")}</small>
          <NewsQuotePrice quote={quote} />
        </div>
      </div>

      {tradeable.length ? (
        <div className="news-symbol-contracts">
          {contracts.map((contract) => (
            <span
              className="news-symbol-contract"
              data-reference={contract.reference_only || undefined}
              key={`${contract.venue}:${contract.venue_symbol}`}
            >
              <code>
                {contract.venue}:{contract.venue_symbol}
              </code>
              {contract.quote_asset ? <small>{contract.quote_asset}</small> : null}
              {/* #91: kept on screen rather than filtered out — the page is where "what is this" is asked. */}
              {contract.reference_only ? <small>仅参考</small> : null}
            </span>
          ))}
        </div>
      ) : null}

      <NewsSourceLine
        note="报价与合约是两个来源：合约来自标的表快照，报价来自 /api/news/quotes，陈旧就说陈旧，永不显示 0。"
        path="GET /api/news/symbols/{base} → contracts · tradeable · normalization"
      />
    </Card>
  );
}
