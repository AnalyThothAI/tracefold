import { displayAssetRefs } from "@features/news/model/newsLabels";
import { NewsAssetChips } from "@features/news/ui/chrome/NewsAssetChips";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

const LISTED = { base_symbol: "HYPE", listed: true, symbol: "HYPE", venue: "hl.perp" };
const UNLISTED = { base_symbol: "SPOT", listed: false, symbol: "SPOT", venue: null };

describe("NewsAssetChips", () => {
  it("names the venue for a tag that resolves, and marks one that does not", () => {
    render(<NewsAssetChips assets={[LISTED, UNLISTED]} />);

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

  it("renders nothing rather than an empty container when an Event grounded on nothing", () => {
    const { container } = render(<NewsAssetChips assets={[]} />);

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

  it("keeps the four-chip cap the row was laid out for", () => {
    const many = ["A", "B", "C", "D", "E", "F"];

    expect(displayAssetRefs(many, []).map((asset) => asset.symbol)).toEqual(["A", "B", "C", "D"]);
  });
});
