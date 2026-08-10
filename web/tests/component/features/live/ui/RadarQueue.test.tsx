import { RadarQueue, type TokenRadarSnapshot } from "@features/live";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("RadarQueue", () => {
  it("renders a rich server-ordered Top 50 with one Case action per item and no hydration", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const snapshot = fixture();
    const { container } = renderQueue(snapshot);
    const rows = screen.getAllByRole("listitem");

    expect(screen.getByRole("heading", { name: "Radar" })).toBeInTheDocument();
    expect(screen.getByText("Showing 50 / 63 eligible")).toBeInTheDocument();
    expect(rows).toHaveLength(50);
    expect(rows.map((row) => row.querySelector("strong")?.textContent)).toEqual(
      Array.from({ length: 50 }, (_, index) => `$TOKEN${index + 1}`),
    );
    expect(rows[0]).toHaveTextContent("Token 1 Network");
    expect(rows[0]).toHaveTextContent("solana · token-1");
    expect(rows[0]).toHaveTextContent("$0.00003281");
    expect(rows[0]).toHaveTextContent("+12%");
    expect(rows[0]).toHaveTextContent("$1.3M");

    const actions = screen.getAllByRole("link", { name: "Open Token Case" });
    expect(actions).toHaveLength(50);
    expect(actions[0]).toHaveAttribute(
      "href",
      "/token/Asset/asset%3Asolana%3Atoken%3Atoken-1?window=1h&focus=trigger&trigger_event_id=event-token-1",
    );
    expect(actions[49]).toBeVisible();
    expect(container.querySelector(".live-radar-queue")).not.toHaveClass(
      "live-radar-queue--delayed",
    );
    const icon = screen.getByRole("img", { name: "Token 1 Network icon" });
    expect(icon).toHaveAttribute("src", `/api/token-images/${"a".repeat(64)}`);
    expect(icon).toHaveAttribute("decoding", "async");
    expect(container.querySelector("details")).toBeNull();
    expect(screen.getAllByRole("button", { name: /Copy TOKEN\d+ contract address/ })).toHaveLength(
      50,
    );
    expect(container.querySelectorAll("*").length).toBeLessThanOrEqual(1_100);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses a fixed icon box with a letter fallback and em dashes for missing market facts", () => {
    const snapshot = fixture(1);
    snapshot.items[0].target.logo_url = null;
    snapshot.items[0].target.name = null;
    snapshot.items[0].market = {
      status: "unavailable",
      price_usd: null,
      price_change_since_signal: null,
      market_cap_usd: null,
      observed_at_ms: null,
    };

    const { container } = renderQueue(snapshot);
    const row = screen.getByRole("listitem");
    const fallback = container.querySelector(".live-radar-icon-fallback");

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(fallback).toHaveTextContent("T");
    expect(within(row).getByRole("group", { name: "Price —" })).toHaveTextContent("—");
    expect(within(row).getByRole("group", { name: "Since signal —" })).toHaveTextContent("—");
    expect(within(row).getByRole("group", { name: "Market cap —" })).toHaveTextContent("—");
  });

  it("presents market and evidence as labelled scan groups instead of a clipped sentence", () => {
    renderQueue(fixture(1));
    const row = screen.getByRole("listitem");
    const market = screen.getByLabelText("TOKEN1 market facts");
    const evidence = screen.getByLabelText("TOKEN1 evidence");

    expect(within(market).getByRole("group", { name: "Price $0.00003281" })).toBeVisible();
    expect(within(market).getByRole("group", { name: "Since signal +12%" })).toBeVisible();
    expect(within(market).getByRole("group", { name: "Market cap $1.3M" })).toBeVisible();
    expect(
      within(evidence).getByRole("group", { name: "Attention +5, 2 to 7 mentions" }),
    ).toBeVisible();
    expect(
      within(evidence).getByRole("group", {
        name: "Independent evidence, 4 authors, 5 texts",
      }),
    ).toBeVisible();
    expect(
      within(evidence).getByRole("group", {
        name: "Formation quality, 2m, 10% duplicates",
      }),
    ).toBeVisible();
    expect(row).toHaveTextContent("+5 · 2→7 mentions");
    expect(row).toHaveTextContent("4 authors · 5 texts");
    expect(row).toHaveTextContent("2m · 10% duplicates");
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

  it("never fabricates a GMGN destination for an unsupported chain", () => {
    const snapshot = fixture(1);
    snapshot.items[0].target.chain = "robinhood";
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
    expect(screen.getByRole("listitem")).toHaveTextContent("solana");
    expect(screen.getByRole("listitem")).not.toHaveTextContent("solana ·");
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

  it("keeps the last good snapshot and reports a refresh delay", () => {
    const { container } = renderQueue(fixture(), new Error("offline"));

    expect(screen.getByText("$TOKEN1")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("更新延迟");
    expect(screen.queryByText("offline")).not.toBeInTheDocument();
    expect(container.querySelector(".live-radar-queue")).toHaveClass("live-radar-queue--delayed");
  });

  it("keeps one contained row slot stable as an empty publication becomes eligible", () => {
    const emptySnapshot = { ...fixture(), eligible_total: 0, evidence_as_of_ms: 0, items: [] };
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
    const list = screen.getByRole("list", { name: "Radar priority queue" });
    const emptyState = screen.getByText("No eligible cases");
    expect(emptyState).toBeInTheDocument();
    expect(emptyState.closest(".live-radar-item")?.parentElement).toBe(list);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.queryByRole("link", { name: "Open Token Case" })).not.toBeInTheDocument();
    const evidenceClock = container.querySelector(".live-radar-header time");
    expect(evidenceClock).not.toBeNull();
    expect(evidenceClock).toHaveTextContent("no evidence");
    const emptySlot = container.querySelector(".live-radar-item");
    expect(emptySlot).not.toBeNull();
    const contractSurface = emptySlot?.querySelector(".live-radar-contract");
    const stableContractLink = contractSurface?.querySelector("a");
    const stableCopyButton = contractSurface?.querySelector("button");
    expect(stableContractLink).not.toBeNull();
    expect(stableCopyButton).not.toBeNull();
    expect(stableContractLink).not.toHaveAttribute("href");
    expect(stableCopyButton).toBeDisabled();
    const emptySlotClassName = emptySlot?.className;
    const emptyChildren = [...(emptySlot?.children ?? [])];
    expect(emptySlot).toBeVisible();
    expect(emptySlot).not.toHaveAttribute("hidden");
    expect(container.querySelectorAll(".live-radar-item")).toHaveLength(1);

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

    expect(screen.getByRole("list", { name: "Radar priority queue" })).toBe(list);
    expect(screen.queryByText("No eligible cases")).not.toBeInTheDocument();
    expect(container.querySelector(".live-radar-header time")).toBe(evidenceClock);
    expect(evidenceClock).not.toHaveTextContent("no evidence");
    expect(container.querySelector(".live-radar-item")).toBe(emptySlot);
    expect(container.querySelector(".live-radar-item")?.className).toBe(emptySlotClassName);
    expect([...(container.querySelector(".live-radar-item")?.children ?? [])]).toEqual(
      emptyChildren,
    );
    expect(container.querySelector(".live-radar-contract a")).toBe(stableContractLink);
    expect(container.querySelector(".live-radar-contract button")).toBe(stableCopyButton);
    expect(stableContractLink).toHaveAttribute("href", "https://gmgn.ai/sol/token/token-1");
    expect(stableCopyButton).not.toBeDisabled();
    expect(screen.getAllByRole("listitem")).toHaveLength(50);
    expect(screen.getAllByRole("link", { name: "Open Token Case" })).toHaveLength(50);
    expect(container.querySelectorAll("*").length).toBeLessThanOrEqual(1_100);
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
    schema_version: "token_radar_snapshot_v2",
    evidence_as_of_ms: 1_778_426_440_000,
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
    triggered_at_ms: 1_778_426_430_000,
    why_now: { current_mentions: 7, prior_mentions: 2, mention_delta: mentionDelta },
    evidence: {
      new_independent_author_count: 4,
      independent_text_count: 5,
      time_to_nth_author_ms: 90_000,
      duplicate_share: 0.1,
    },
    market: {
      status: "confirmed" as const,
      price_usd: 0.00003281,
      price_change_since_signal: 0.12,
      market_cap_usd: 1_250_000,
      observed_at_ms: 1_778_426_435_000,
    },
    counter_evidence: null,
  };
}
