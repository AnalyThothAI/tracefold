import {
  NewsQuoteChange,
  NewsQuotePrice,
  NewsReactionValue,
} from "@features/news/ui/chrome/NewsQuoteValue";
import { cleanup, render, screen } from "@testing-library/react";
import { newsQuoteFixture, newsReactionFixture } from "@tests/fixtures/newsFixture";
import { afterEach, describe, expect, it } from "vitest";

/**
 * The one failure this whole plane exists to avoid: a provider outage that looks like a flat market (#88).
 *
 * A stale quote keeps its number and says it is stale; an unavailable one says so in words. Neither renders
 * as `0` or `0.00%`, and an unfinished measurement is never drawn as a zero return.
 */
describe("NewsQuotePrice / NewsQuoteChange", () => {
  afterEach(cleanup);

  it("shows a fresh price and its change, each naming the contract behind it", () => {
    render(
      <>
        <NewsQuotePrice quote={newsQuoteFixture()} />
        <NewsQuoteChange quote={newsQuoteFixture()} />
      </>,
    );

    expect(screen.getByText("68,123.40")).toBeInTheDocument();
    expect(screen.getByText("+1.52%")).toBeInTheDocument();
    const title = screen.getByText("68,123.40").getAttribute("title") ?? "";
    expect(title).toContain("binance.perp:BTCUSDT");
    expect(title).toContain("最新成交价");
    expect(title).toContain("滚动 24H");
  });

  it("keeps a stale quote visible and marked rather than blanking it", () => {
    const stale = newsQuoteFixture({
      effective_age_ms: 90_000,
      received_age_ms: 90_000,
      state: "stale",
      state_zh: "报价陈旧",
    });
    render(<NewsQuotePrice quote={stale} />);

    // The number stays: a blanked price looks exactly like a market that did not move. Staleness is carried
    // by the server state and a nearby age marker; hover is supporting detail, never the only signal.
    const price = screen.getByText("68,123.40");
    expect(price.closest("[data-state='stale']")).toBeInTheDocument();
    expect(screen.getByText("陈旧 2m")).toBeInTheDocument();
    expect(price.getAttribute("title")).toContain("报价陈旧");
  });

  it("names provider, receipt, and reference clocks in the tooltip", () => {
    render(<NewsQuotePrice quote={newsQuoteFixture()} />);

    const title = screen.getByText("68,123.40").getAttribute("title") ?? "";
    expect(title).toContain("提供方时间");
    expect(title).toContain("Tracefold 接收");
    expect(title).toContain("24H 参考");
    expect(title).toContain("有效时效");
  });

  it("renders a price with no percentage, and does not claim a window it has no number for", () => {
    // #109: a Binance price read carries no day change until a day read has cached its reference.
    const quote = newsQuoteFixture({ change_pct: null });
    render(
      <>
        <NewsQuotePrice quote={quote} />
        <NewsQuoteChange quote={quote} />
      </>,
    );

    expect(screen.getByText("68,123.40")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    const title = screen.getByText("68,123.40").getAttribute("title") ?? "";
    expect(title).toContain("binance.perp:BTCUSDT");
    expect(title).not.toContain("滚动 24H变动");
  });

  it("says unavailable and unlisted in words, never as a price", () => {
    const { rerender } = render(
      <NewsQuotePrice
        quote={newsQuoteFixture({ price: null, state: "unavailable", state_zh: "暂无报价" })}
      />,
    );
    expect(screen.getByText("暂无报价")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();

    rerender(
      <NewsQuotePrice
        quote={newsQuoteFixture({
          price: null,
          state: "unlisted",
          state_zh: "无可交易合约",
          venue: null,
        })}
      />,
    );
    expect(screen.getByText("无可交易合约")).toBeInTheDocument();
  });

  it("renders no change at all for a quote that has none", () => {
    const { container } = render(
      <NewsQuoteChange
        quote={newsQuoteFixture({ price: null, state: "unavailable", state_zh: "暂无报价" })}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});

describe("NewsReactionValue", () => {
  afterEach(cleanup);

  it("renders a completed horizon as a signed percentage", () => {
    render(<NewsReactionValue horizon="1h" reaction={newsReactionFixture()} />);

    expect(screen.getByText("1H")).toBeInTheDocument();
    expect(screen.getByText("+1.52%")).toBeInTheDocument();
  });

  it("calls an unmatured horizon 未到期 instead of a zero return", () => {
    render(
      <NewsReactionValue
        horizon="4h"
        reaction={newsReactionFixture({
          return_4h_bps: null,
          state: "partial",
          state_zh: "1H 已出",
        })}
      />,
    );

    expect(screen.getByText("未到期")).toBeInTheDocument();
    expect(screen.queryByText("0.00%")).not.toBeInTheDocument();
  });

  it("carries the server's reason when a horizon cannot be computed at all", () => {
    render(
      <NewsReactionValue
        horizon="1h"
        reaction={newsReactionFixture({
          priced_n: 0,
          return_1h_bps: null,
          return_4h_bps: null,
          state: "unavailable",
          state_zh: "无法计算",
          unavailable_reason: "no_candle_within_gap",
          unavailable_reason_zh: "该时段没有成交 K 线，不做前向填充",
        })}
      />,
    );

    expect(screen.getByText("该时段没有成交 K 线，不做前向填充")).toBeInTheDocument();
  });

  it("renders nothing numeric when there is no reaction at all", () => {
    render(<NewsReactionValue horizon="1h" reaction={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
