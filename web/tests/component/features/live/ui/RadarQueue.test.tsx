import { RadarQueue, type TokenRadarSnapshot } from "@features/live";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
    expect(rows[0]).toHaveTextContent("Price $0.00003281");
    expect(rows[0]).toHaveTextContent("+12% since signal");
    expect(rows[0]).toHaveTextContent("MCap $1.3M");
    expect(rows[0]).toHaveTextContent(
      "+5 mentions · 2→7 · 4 new authors · 5 independent texts · formed in 2m · 10% duplicates",
    );

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
    expect(container.querySelector("details, button")).toBeNull();
    expect(container.querySelectorAll("*").length).toBeLessThanOrEqual(1_000);
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
    expect(row).toHaveTextContent("Price —");
    expect(row).toHaveTextContent("— since signal");
    expect(row).toHaveTextContent("MCap —");
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
    expect(screen.getAllByRole("listitem")).toHaveLength(50);
    expect(screen.getAllByRole("link", { name: "Open Token Case" })).toHaveLength(50);
    expect(container.querySelectorAll("*").length).toBeLessThanOrEqual(1_000);
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
