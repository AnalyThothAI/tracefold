import { RadarQueue, type TokenRadarSnapshot } from "@features/live";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("RadarQueue", () => {
  it("renders a rich server-ordered Top 50 with one whole-card Case link per item and no hydration", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const snapshot = fixture();
    const { container } = renderQueue(snapshot);
    const rows = screen.getAllByRole("listitem");

    expect(screen.getByRole("heading", { name: "Radar" })).toBeInTheDocument();
    expect(screen.getByText("4h causal change · newest qualification first")).toBeInTheDocument();
    expect(screen.getByText("63 eligible · showing 50 / 50")).toBeInTheDocument();
    expect(screen.getByText(/Social evidence through/)).toBeInTheDocument();
    expect(rows).toHaveLength(50);
    expect(rows.map((row) => row.querySelector("strong")?.textContent)).toEqual(
      Array.from({ length: 50 }, (_, index) => `$TOKEN${index + 1}`),
    );
    expect(rows[0]).toHaveTextContent("Token 1 Network");
    expect(rows[0]).toHaveTextContent("Solana · token-1 · GMGN ↗");
    expect(rows[0]).toHaveTextContent("$0.00003281");
    expect(rows[0]).toHaveTextContent("+12%");
    expect(rows[0]).toHaveTextContent("$1.3M");

    const actions = screen.getAllByRole("link", { name: /Open TOKEN\d+ Token Case/ });
    expect(actions).toHaveLength(50);
    expect(actions[0]).toHaveAttribute(
      "href",
      "/token/Asset/asset%3Asolana%3Atoken%3Atoken-1?window=4h&focus=trigger&trigger_event_id=event-token-1",
    );
    expect(actions[49]).toBeVisible();
    expect(actions[0]).toHaveClass("live-radar-card-link");
    const icon = screen.getByRole("img", { name: "Token 1 Network icon" });
    expect(icon).toHaveAttribute("src", `/api/token-images/${"a".repeat(64)}`);
    expect(icon).toHaveAttribute("decoding", "async");
    expect(container.querySelector("details")).toBeNull();
    expect(screen.getAllByRole("button", { name: /Copy TOKEN\d+ contract address/ })).toHaveLength(
      50,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses a fixed icon fallback and explains unavailable market facts", () => {
    const snapshot = fixture(1);
    snapshot.items[0].target.logo_url = null;
    snapshot.items[0].target.name = null;
    snapshot.items[0].market = {
      price_usd: null,
      price_observed_at_ms: null,
      price_change_since_signal: null,
      market_cap_usd: null,
      market_cap_observed_at_ms: null,
    };

    const { container } = renderQueue(snapshot);
    const row = screen.getByRole("listitem");
    const fallback = container.querySelector(".live-radar-icon-fallback");

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(fallback).toHaveTextContent("T");
    expect(
      within(row).getByRole("group", { name: "Price No fresh quote, No observation" }),
    ).toBeVisible();
    expect(within(row).getByRole("group", { name: "Since signal No signal change" })).toBeVisible();
    expect(
      within(row).getByRole("group", { name: "Market cap No fresh cap, No observation" }),
    ).toBeVisible();
  });

  it("renders an address-only asset without fabricating a ticker", () => {
    const address = "J7o48eA9qftqHpod2CsUbBH4q1Tzq3doTRXFDA4wpump";
    const snapshot = fixture(1);
    snapshot.items[0].target.symbol = address;
    snapshot.items[0].target.name = null;
    snapshot.items[0].target.address = address;

    renderQueue(snapshot);

    expect(screen.getByText("J7o48eA9...4wpump")).toBeVisible();
    expect(screen.getByText("Contract address")).toBeVisible();
    expect(screen.queryByText(`$${address}`)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open J7o48eA9...4wpump on GMGN" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Copy J7o48eA9...4wpump contract address" }),
    ).toBeVisible();
  });

  it("presents market and evidence as labelled scan groups instead of a clipped sentence", () => {
    const snapshot = fixture(1);
    renderQueue(snapshot);
    const row = screen.getByRole("listitem");
    const market = screen.getByLabelText("TOKEN1 market facts");
    const evidence = screen.getByLabelText("TOKEN1 evidence");

    expect(
      within(market).getByRole("group", { name: /Price \$0\.00003281, Observed/ }),
    ).toBeVisible();
    expect(within(market).getByRole("group", { name: "Since signal +12%" })).toBeVisible();
    expect(
      within(market).getByRole("group", { name: /Market cap \$1\.3M, Observed/ }),
    ).toBeVisible();
    expect(screen.getByTitle("Price observation time")).toHaveAttribute(
      "datetime",
      new Date(snapshot.items[0].market.price_observed_at_ms!).toISOString(),
    );
    expect(screen.getByTitle("Market-cap observation time")).toHaveAttribute(
      "datetime",
      new Date(snapshot.items[0].market.market_cap_observed_at_ms!).toISOString(),
    );
    expect(
      within(evidence).getByRole("group", { name: "Mentions 2 to 7, increase 5" }),
    ).toBeVisible();
    expect(
      within(evidence).getByRole("group", {
        name: "Independent evidence, 4 independent authors, 5 independent texts",
      }),
    ).toBeVisible();
    expect(
      within(evidence).getByRole("group", {
        name: "Formation quality, formed in 2m, 10% duplicate text",
      }),
    ).toBeVisible();
    expect(row).toHaveTextContent("2→7 · +5");
    expect(row).toHaveTextContent("4 authors · 5 texts");
    expect(row).toHaveTextContent("2m to form · 10% duplicate");
    expect(screen.getByTitle("Trigger source-event time")).toHaveTextContent("Source");
    expect(screen.getByTitle("Qualification time")).toHaveTextContent("Qualified");
    expect(row).not.toHaveTextContent(/new authors/i);
  });

  it("copies the full contract address and opens the supported asset on GMGN", async () => {
    const address = "0x514910771af9ca656af840dff83e8264ecf986ca";
    let resolveCopy!: () => void;
    const writeText = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveCopy = resolve;
        }),
    );
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const snapshot = fixture(1);
    snapshot.items[0].target.chain = "eip155:1";
    snapshot.items[0].target.address = address;
    renderQueue(snapshot);

    const gmgn = screen.getByRole("link", { name: "Open TOKEN1 on GMGN" });
    expect(gmgn).toHaveAttribute("href", `https://gmgn.ai/eth/token/${address}`);
    expect(gmgn).toHaveAttribute("target", "_blank");
    expect(gmgn).toHaveAttribute("rel", "noreferrer");

    const copy = screen.getByRole("button", { name: "Copy TOKEN1 contract address" });
    fireEvent.click(copy);

    expect(copy).toBeDisabled();
    expect(copy).toHaveAccessibleName("TOKEN1 contract address copying");
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(address));
    resolveCopy();
    await waitFor(() => expect(copy).toHaveTextContent("Copied"));
    expect(copy).toHaveTextContent("Copied");
    expect(copy).toHaveAccessibleName("TOKEN1 contract address copied");
  });

  it("opens a Robinhood Chain asset on GMGN and names the network clearly", () => {
    const snapshot = fixture(1);
    snapshot.items[0].target.chain = "robinhood";
    renderQueue(snapshot);

    expect(screen.getByRole("link", { name: "Open TOKEN1 on GMGN" })).toHaveAttribute(
      "href",
      "https://gmgn.ai/robinhood/token/token-1",
    );
    expect(screen.getByRole("listitem")).toHaveTextContent("Robinhood Chain");
    expect(screen.getByRole("button", { name: "Copy TOKEN1 contract address" })).toBeVisible();
  });

  it("never fabricates a GMGN destination for an unknown chain", () => {
    const snapshot = fixture(1);
    snapshot.items[0].target.chain = "eip155:999999";
    renderQueue(snapshot);

    expect(screen.queryByRole("link", { name: "Open TOKEN1 on GMGN" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy TOKEN1 contract address" })).toBeVisible();
  });

  it("reports a clipboard failure without a compatibility fallback", async () => {
    vi.stubGlobal("navigator", {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    renderQueue(fixture(1));

    fireEvent.click(screen.getByRole("button", { name: "Copy TOKEN1 contract address" }));

    expect(
      await screen.findByRole("button", { name: "TOKEN1 contract address copy failed" }),
    ).toHaveTextContent("Copy failed");
  });

  it("does not render address controls or a dangling separator without an address", () => {
    const snapshot = fixture(1);
    snapshot.items[0].target.address = null;
    renderQueue(snapshot);

    expect(screen.queryByRole("link", { name: "Open TOKEN1 on GMGN" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Copy TOKEN1 contract address" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("listitem")).toHaveTextContent("Solana");
    expect(screen.getByRole("listitem")).not.toHaveTextContent("Solana ·");
  });

  it("allows a new logo source to recover after the previous image failed", () => {
    const snapshot = fixture(1);
    const { container, rerender } = renderQueue(snapshot);
    const image = container.querySelector(".live-radar-icon img");
    expect(image).not.toBeNull();

    fireEvent.error(image!);
    expect(image).toHaveAttribute("hidden");

    const refreshed = fixture(1);
    refreshed.items[0].target.logo_url = `/api/token-images/${"b".repeat(64)}`;
    rerender(
      <MemoryRouter>
        <RadarQueue
          bootstrapError={false}
          bootstrapLoading={false}
          error={null}
          isLoading={false}
          isRefreshing={false}
          snapshot={refreshed}
          onRetry={vi.fn()}
          onSessionRetry={vi.fn()}
          sessionAvailable={true}
        />
      </MemoryRouter>,
    );

    expect(container.querySelector(".live-radar-icon img")).toBe(image);
    expect(image).not.toHaveAttribute("hidden");
    expect(image).toHaveAttribute("src", refreshed.items[0].target.logo_url);
  });

  it("keeps the last good snapshot and hides a cached refresh failure", () => {
    const { container } = renderQueue(fixture(), new Error("offline"));

    expect(screen.getByText("$TOKEN1")).toBeInTheDocument();
    expect(screen.queryByText("更新延迟")).not.toBeInTheDocument();
    expect(screen.queryByText("offline")).not.toBeInTheDocument();
    expect(container.querySelector(".live-radar-queue")).toBeInTheDocument();
  });

  it("restores the bounded queue scroll from route interaction state", () => {
    render(
      <MemoryRouter>
        <RadarQueue
          bootstrapError={false}
          bootstrapLoading={false}
          error={null}
          initialScrollTop={720}
          isLoading={false}
          isRefreshing={false}
          snapshot={fixture()}
          onRetry={vi.fn()}
          onSessionRetry={vi.fn()}
          sessionAvailable={true}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("list", { name: "Radar priority queue" })).toHaveProperty(
      "scrollTop",
      720,
    );
  });

  it("renders a truthful empty state and only creates rows for real eligible items", () => {
    const emptySnapshot = {
      ...fixture(),
      eligible_total: 0,
      social_evidence_as_of_ms: 0,
      items: [],
    };
    const { container, rerender } = render(
      <MemoryRouter>
        <RadarQueue
          bootstrapError={false}
          bootstrapLoading={false}
          error={null}
          isLoading={false}
          isRefreshing={false}
          snapshot={emptySnapshot}
          onRetry={vi.fn()}
          onSessionRetry={vi.fn()}
          sessionAvailable={true}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("No eligible cases")).toBeInTheDocument();
    expect(screen.queryByText("Unknown venue")).not.toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Radar priority queue" })).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Token Case/ })).not.toBeInTheDocument();
    const evidenceClock = container.querySelector(".live-radar-header time");
    expect(evidenceClock).not.toBeNull();
    expect(evidenceClock).toHaveTextContent("No social evidence yet");

    rerender(
      <MemoryRouter>
        <RadarQueue
          bootstrapError={false}
          bootstrapLoading={false}
          error={null}
          isLoading={false}
          isRefreshing={false}
          snapshot={fixture()}
          onRetry={vi.fn()}
          onSessionRetry={vi.fn()}
          sessionAvailable={true}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("list", { name: "Radar priority queue" })).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(container.querySelector(".live-radar-header time")).toBe(evidenceClock);
    expect(evidenceClock).not.toHaveTextContent("No social evidence yet");
    expect(screen.getAllByRole("listitem")).toHaveLength(50);
    expect(screen.getAllByRole("link", { name: /Open TOKEN\d+ Token Case/ })).toHaveLength(50);
  });
});

function renderQueue(snapshot: TokenRadarSnapshot, error: Error | null = null) {
  return render(
    <MemoryRouter>
      <RadarQueue
        bootstrapError={false}
        bootstrapLoading={false}
        error={error}
        isLoading={false}
        isRefreshing={Boolean(error)}
        snapshot={snapshot}
        onRetry={vi.fn()}
        onSessionRetry={vi.fn()}
        sessionAvailable={true}
      />
    </MemoryRouter>,
  );
}

function fixture(count = 50): TokenRadarSnapshot {
  return {
    schema_version: "token_radar_snapshot_v5",
    social_evidence_as_of_ms: 1_778_426_440_000,
    eligible_total: count === 50 ? 63 : count,
    items: Array.from({ length: count }, (_, index) =>
      item(`TOKEN${index + 1}`, `token-${index + 1}`, 5 + index),
    ),
  };
}

function item(symbol: string, id: string, mentionDelta: number) {
  return {
    target: {
      target_type: "Asset" as const,
      target_id: `asset:solana:token:${id}`,
      symbol,
      name: `Token ${id.replace("token-", "")} Network`,
      logo_url: `/api/token-images/${"a".repeat(64)}`,
      chain: "solana",
      exchange: null,
      address: id,
    },
    trigger_event_id: `event-${id}`,
    trigger_source_event_at_ms: 1_778_426_430_000,
    qualified_at_ms: 1_778_426_435_000,
    why_now: { current_mentions: 7, prior_mentions: 2, mention_delta: mentionDelta },
    evidence: {
      independent_author_count: 4,
      independent_text_count: 5,
      time_to_nth_author_ms: 90_000,
      duplicate_share: 0.1,
    },
    market: {
      price_usd: 0.00003281,
      price_observed_at_ms: 1_778_426_439_000,
      price_change_since_signal: 0.12,
      market_cap_usd: 1_250_000,
      market_cap_observed_at_ms: 1_778_426_438_000,
    },
  };
}
