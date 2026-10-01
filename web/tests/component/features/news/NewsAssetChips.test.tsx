import { newsAssetKey } from "@features/news/api/newsQueries";
import { NewsAssetChips } from "@features/news/ui/chrome/NewsAssetChips";
import { cleanup, render, screen } from "@testing-library/react";
import { newsQuoteFixture } from "@tests/fixtures/newsFixture";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

/** Every chip is a link now (#207 principle 9), so the chips need a router around them. */
const renderChips = (element: ReactElement) => render(<MemoryRouter>{element}</MemoryRouter>);

const LISTED = {
  base_symbol: "HYPE",
  market_type: "crypto" as const,
  resolution_state: "resolved" as const,
  listed: true,
  symbol: "HYPE",
  venue: "hl.perp",
};
const UNLISTED = {
  base_symbol: "SPOT",
  market_type: "crypto" as const,
  resolution_state: "unlisted" as const,
  listed: false,
  symbol: "SPOT",
  venue: null,
};

describe("NewsAssetChips", () => {
  it("names the venue for a tag that resolves, and marks one that does not", () => {
    renderChips(<NewsAssetChips assets={[LISTED, UNLISTED]} />);

    const chips = screen.getByLabelText("关联资产").querySelectorAll("code");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("hl.perp:HYPE");
    expect(chips[0]).toHaveAttribute("data-resolution", "resolved");
    /*
     * The whole point of #87: the provider tags `SPOT` on a Spot Gold headline and `NEAR` on the words
     * "near-instant". Before this they rendered exactly like a real listing, so a reader could not tell a
     * missed BTC card from a card about a symbol that never existed.
     */
    expect(chips[1]).toHaveTextContent("SPOT");
    expect(chips[1]).toHaveAttribute("data-resolution", "unlisted");
    expect(chips[1].textContent).not.toContain(":");
  });

  it("routes every symbol to its token page, including one that resolved to nothing", () => {
    /*
     * #207 principle 9. The struck-through chip is a link too: `/api/news/symbols/{base}` answers
     * `known: false` for a tag no venue lists, which is the answer a reader following that chip came for —
     * a 404 would make the console's own honesty look like a broken link.
     */
    renderChips(<NewsAssetChips assets={[LISTED, UNLISTED]} />);

    expect(screen.getByText("HYPE").closest("a")).toHaveAttribute("href", "/news/symbols/HYPE");
    expect(screen.getByText("SPOT").closest("a")).toHaveAttribute("href", "/news/symbols/SPOT");
  });

  it("keeps contract addresses visible without linking to an invalid catalogue page", () => {
    const addresses = [`0x${"a".repeat(40)}`, "Z".repeat(44)];
    renderChips(
      <NewsAssetChips
        assets={addresses.map((symbol) => ({
          ...UNLISTED,
          symbol,
          base_symbol: symbol.toUpperCase(),
        }))}
      />,
    );

    for (const address of addresses) {
      const label = screen.getByText(address);
      expect(label).toBeVisible();
      expect(label).toHaveAttribute("title", "未匹配行情标的");
      expect(label.closest("a")).toBeNull();
    }
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("keys the link on the collapsed identity, not the provider's prefixed spelling", () => {
    // `XYZ-UNITREE` and `UNITREE` are one instrument; the token page is keyed on the base the Gate stored.
    renderChips(
      <NewsAssetChips
        assets={[
          {
            base_symbol: "UNITREE",
            market_type: "crypto" as const,
            resolution_state: "resolved" as const,
            listed: true,
            symbol: "XYZ-UNITREE",
            venue: "hl.xyz",
          },
        ]}
      />,
    );

    expect(screen.getByText("XYZ-UNITREE").closest("a")).toHaveAttribute(
      "href",
      "/news/symbols/UNITREE",
    );
  });

  it("shows the first few chips and counts the overflow", () => {
    const assets = ["A", "B", "C", "D", "E"].map((symbol) => ({
      base_symbol: symbol,
      market_type: "crypto" as const,
      resolution_state: "resolved" as const,
      listed: true,
      symbol,
      venue: "hl.perp",
    }));
    renderChips(<NewsAssetChips assets={assets} max={3} />);

    expect(screen.getByLabelText("关联资产").querySelectorAll("code")).toHaveLength(3);
    expect(screen.getByText("+2")).toBeInTheDocument();
  });

  it("shows current price and rolling 24H change when the feed asks for the compact quote", () => {
    renderChips(
      <NewsAssetChips
        assets={[LISTED]}
        quotes={{
          [newsAssetKey("crypto", "HYPE")]: newsQuoteFixture({
            base_symbol: "HYPE",
            change_pct: 39.38,
            price: "0.16059",
            requested_symbol: "HYPE",
            symbol: "HYPE",
            venue_symbol: "HYPEUSDT",
          }),
        }}
        withPrice
      />,
    );

    const chip = screen.getByText("HYPE").closest("code");
    expect(chip).toHaveTextContent("0.16059");
    expect(chip).toHaveTextContent("+39.38%");
  });

  it("omits missing compact values instead of printing placeholder dashes in a Feed row", () => {
    const waiting = renderChips(<NewsAssetChips assets={[LISTED]} withPrice />);

    expect(waiting.container.querySelector("code")).toHaveTextContent("hl.perp:HYPE");
    expect(waiting.container.querySelector("code")).not.toHaveTextContent("—");
    waiting.unmount();

    const noDayChange = renderChips(
      <NewsAssetChips
        assets={[LISTED]}
        quotes={{
          [newsAssetKey("crypto", "HYPE")]: newsQuoteFixture({
            change_pct: null,
            price: "0.16059",
          }),
        }}
        withPrice
      />,
    );
    const chip = noDayChange.container.querySelector("code");
    expect(chip).toHaveTextContent("0.16059");
    expect(chip).not.toHaveTextContent("%");
    expect(chip).not.toHaveTextContent("—");
  });

  it("keeps same-ticker markets and their prices separate", () => {
    renderChips(
      <NewsAssetChips
        assets={[
          {
            ...LISTED,
            symbol: "V",
            base_symbol: "V",
            market_type: "equity",
            venue: "hl.xyz",
            venue_symbol: "xyz:V",
          },
          {
            ...LISTED,
            symbol: "V",
            base_symbol: "V",
            venue: "binance.perp",
            venue_symbol: "VUSDT",
          },
        ]}
        quotes={{
          [newsAssetKey("equity", "V")]: newsQuoteFixture({
            market_type: "equity",
            requested_symbol: "V",
            price: "321",
          }),
          [newsAssetKey("crypto", "V")]: newsQuoteFixture({
            market_type: "crypto",
            requested_symbol: "V",
            price: "0.0123",
          }),
        }}
        withPrice
      />,
    );

    const chips = screen.getByLabelText("关联资产").querySelectorAll("code");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("equity · hl.xyz:V321");
    expect(chips[0]).not.toHaveTextContent("0.0123");
    expect(chips[1]).toHaveTextContent("crypto · binance.perp:V0.0123");
    expect(chips[1]).not.toHaveTextContent("321");
  });

  it("keeps reference-only and unresolved-market assets visible without marking them unlisted", () => {
    renderChips(
      <NewsAssetChips
        assets={[
          {
            ...UNLISTED,
            symbol: "SEI",
            base_symbol: "SEI",
            market_type: "equity",
            resolution_state: "reference_only",
            venue: "us.listed",
            venue_symbol: "SEI",
          },
          {
            ...UNLISTED,
            symbol: "NEWS",
            base_symbol: "NEWS",
            market_type: "unknown",
            resolution_state: "unresolved_market",
          },
        ]}
        quotes={{
          [newsAssetKey("unknown", "NEWS")]: newsQuoteFixture({
            market_type: "unknown",
            price: "999",
          }),
        }}
        withPrice
      />,
    );

    const chips = screen.getByLabelText("关联资产").querySelectorAll("code");
    expect(chips[0]).toHaveAttribute("data-resolution", "reference_only");
    expect(chips[0]).toHaveTextContent("equity · us.listed:SEI");
    expect(chips[1]).toHaveAttribute("data-resolution", "unresolved_market");
    expect(chips[1]).toHaveTextContent("NEWS · 市场未定");
    expect(chips[1]).not.toHaveTextContent("999");
    expect(screen.getByText("NEWS").closest("a")).toHaveAttribute("title", "市场未确定，暂不报价");
  });

  it("renders nothing rather than an empty container when an Event grounded on nothing", () => {
    const { container } = renderChips(<NewsAssetChips assets={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});
