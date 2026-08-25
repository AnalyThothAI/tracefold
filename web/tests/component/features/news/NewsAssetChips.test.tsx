import { displayAssetRefs } from "@features/news/model/newsLabels";
import { NewsAssetChips } from "@features/news/ui/chrome/NewsAssetChips";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

/** Every chip is a link now (#207 principle 9), so the chips need a router around them. */
const renderChips = (element: ReactElement) => render(<MemoryRouter>{element}</MemoryRouter>);

const LISTED = { base_symbol: "HYPE", listed: true, symbol: "HYPE", venue: "hl.perp" };
const UNLISTED = { base_symbol: "SPOT", listed: false, symbol: "SPOT", venue: null };

describe("NewsAssetChips", () => {
  it("names the venue for a tag that resolves, and marks one that does not", () => {
    renderChips(<NewsAssetChips assets={[LISTED, UNLISTED]} />);

    const chips = screen.getByLabelText("关联资产").querySelectorAll("code");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("hl.perp:HYPE");
    expect(chips[0]).toHaveAttribute("data-listed", "true");
    /*
     * The whole point of #87: the provider tags `SPOT` on a Spot Gold headline and `NEAR` on the words
     * "near-instant". Before this they rendered exactly like a real listing, so a reader could not tell a
     * missed BTC card from a card about a symbol that never existed.
     */
    expect(chips[1]).toHaveTextContent("SPOT");
    expect(chips[1]).not.toHaveAttribute("data-listed");
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

  it("keys the link on the collapsed identity, not the provider's prefixed spelling", () => {
    // `XYZ-UNITREE` and `UNITREE` are one instrument; the token page is keyed on the base the Gate stored.
    renderChips(
      <NewsAssetChips
        assets={[{ base_symbol: "UNITREE", listed: true, symbol: "XYZ-UNITREE", venue: "hl.xyz" }]}
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
      listed: true,
      symbol,
      venue: "hl.perp",
    }));
    renderChips(<NewsAssetChips assets={assets} max={3} />);

    expect(screen.getByLabelText("关联资产").querySelectorAll("code")).toHaveLength(3);
    expect(screen.getByText("+2")).toBeInTheDocument();
  });

  it("renders nothing rather than an empty container when an Event grounded on nothing", () => {
    const { container } = renderChips(<NewsAssetChips assets={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe("displayAssetRefs", () => {
  it("falls back to unlisted for a tag the server did not resolve", () => {
    // A response served before #87, or one whose Event carried a tag the resolver never saw. An unknown tag
    // must read as "we cannot place this", never as a confirmed listing.
    expect(displayAssetRefs(["HYPE", "MYSTERY"], [LISTED])).toEqual([
      LISTED,
      { base_symbol: "MYSTERY", listed: false, symbol: "MYSTERY", venue: null },
    ]);
    expect(displayAssetRefs(["HYPE"], undefined)).toEqual([
      { base_symbol: "HYPE", listed: false, symbol: "HYPE", venue: null },
    ]);
  });

  it("matches the provider's prefixed form against the resolved tag", () => {
    // The Gate stores `XYZ-UNITREE`; the resolver answers about `UNITREE`. They are the same instrument.
    const unitree = { base_symbol: "UNITREE", listed: true, symbol: "UNITREE", venue: "hl.xyz" };

    expect(displayAssetRefs(["XYZ-UNITREE"], [unitree])).toEqual([unitree]);
  });

  it("resolves every tag and leaves the cap to the surface rendering them", () => {
    // A row shows three and counts the rest; the detail page lists them all. Capping here would have made
    // "+2" mean "+2 of the four we kept", which is not a number about the Event.
    const many = ["A", "B", "C", "D", "E", "F"];

    expect(displayAssetRefs(many, []).map((asset) => asset.symbol)).toEqual(many);
  });
});
