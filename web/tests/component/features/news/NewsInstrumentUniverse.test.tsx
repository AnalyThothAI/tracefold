import { InstrumentUniverse } from "@features/news/ui/status/NewsInstrumentUniverse";
import { cleanup, render, screen, within } from "@testing-library/react";
import { newsStatusFixture } from "@tests/fixtures/newsFixture";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

function renderUniverse(overrides: Record<string, unknown> = {}) {
  const status = newsStatusFixture();
  return render(
    <InstrumentUniverse
      status={{ ...status, instruments: { ...status.instruments!, ...overrides } }}
    />,
  );
}

describe("InstrumentUniverse", () => {
  it("shows the whole summary, so a field the server adds cannot go unnoticed", () => {
    renderUniverse();

    // `by_class` and `dangling_aliases` were served for a whole release without appearing anywhere (#89 →
    // #87 review); the card renders every field the summary carries — four figures, with the three
    // supporting counts (delisted, venues, snapshot time) as their footnotes.
    for (const caption of ["在交易合约", "base 符号", "参考目录", "悬空别名"]) {
      expect(screen.getByText(caption)).toBeInTheDocument();
    }
    expect(screen.getByText(/已下架/)).toBeInTheDocument();
    expect(screen.getByText(/场所 /)).toBeInTheDocument();
    expect(screen.getByText("2,344")).toBeInTheDocument();
    // The reference tier is counted apart from the traded one on purpose: 13k US tickers folded into
    // `在交易合约` would make that figure mean something it does not (#91).
    expect(screen.getByText("13,134")).toBeInTheDocument();
    expect(within(screen.getByLabelText("按资产类别")).getByText("equity")).toBeInTheDocument();
    expect(within(screen.getByLabelText("按场所")).getByText("binance.perp")).toBeInTheDocument();
  });

  it("stays quiet at zero dangling aliases and takes the caution tone above it", () => {
    /*
     * The alarm this card exists for: a seed alias pointing at a symbol no venue lists resolves to nothing,
     * silently — `1810.HK -> XIAOMI` went unnoticed for a week that way (#89). Amber, never red: red is
     * 利多 in this console.
     */
    const { container: quiet } = renderUniverse({ dangling_aliases: 0 });
    expect(quiet.querySelector('[data-tone="caution"]')).toBeNull();

    cleanup();
    const { container: loud } = renderUniverse({ dangling_aliases: 3 });
    const toned = loud.querySelector('[data-tone="caution"]')!;
    expect(toned).not.toBeNull();
    expect(toned).toHaveTextContent("悬空别名");
    expect(toned).toHaveTextContent("3");
  });

  it("says so plainly before the first snapshot rather than showing a screen of zeroes", () => {
    renderUniverse({ last_snapshot_ms: null });

    expect(screen.getByText(/还没有快照落地/)).toBeInTheDocument();
    expect(screen.queryByText("2,344")).toBeNull();
  });
});
