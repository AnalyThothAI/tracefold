import { RadarQueue, type TokenRadarSnapshot } from "@features/live";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(cleanup);

describe("RadarQueue", () => {
  it("renders the server-prioritized, change-first queue with one Case action per item", () => {
    const snapshot = fixture();
    const { container } = renderQueue(snapshot);
    const rows = screen.getAllByRole("listitem");

    expect(screen.getByRole("heading", { name: "Radar" })).toBeInTheDocument();
    expect(screen.getByText("8 eligible")).toBeInTheDocument();
    expect(rows[0]).toHaveTextContent("+5 · $FIRST · solana · one · +12% since signal");
    expect(rows[1]).toHaveTextContent("+3 · $SECOND · solana · two · +12% since signal");
    expect(rows[0]).toHaveTextContent(
      "mentions 2→7 · 4 new authors · 5 independent texts · formed in 2m · 10% duplicates",
    );

    const actions = screen.getAllByRole("link", { name: "Open Token Case" });
    expect(actions).toHaveLength(2);
    expect(rows.every((row) => row.children.length === 2)).toBe(true);
    const primaryLines = [...container.querySelectorAll(".live-radar-item-primary")];
    const evidenceLines = [...container.querySelectorAll(".live-radar-item-evidence")];
    expect(primaryLines).toHaveLength(8);
    expect(primaryLines.every((line) => line.childElementCount === 2)).toBe(true);
    expect(primaryLines.every((line) => line.firstElementChild?.tagName === "SPAN")).toBe(true);
    expect(primaryLines.every((line) => line.lastElementChild?.tagName === "A")).toBe(true);
    expect(evidenceLines).toHaveLength(8);
    expect(evidenceLines.every((line) => line.childElementCount === 0)).toBe(true);
    expect(actions[0]).toHaveAttribute(
      "href",
      "/token/Asset/asset%3Asolana%3Atoken%3Aone?window=1h&focus=trigger&trigger_event_id=event-one",
    );
    expect(container.querySelector(".live-radar-queue")).not.toHaveClass(
      "live-radar-queue--delayed",
    );
    expect(container.querySelector("img, details, button")).toBeNull();
    expect(container.querySelectorAll("*").length).toBeLessThanOrEqual(500);
  });

  it("keeps the last good snapshot and reports a refresh delay", () => {
    const { container } = renderQueue(fixture(), new Error("offline"));

    expect(screen.getByText(/\$FIRST/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("更新延迟");
    expect(screen.queryByText("offline")).not.toBeInTheDocument();
    expect(container.querySelector(".live-radar-queue")).toHaveClass("live-radar-queue--delayed");
  });

  it("keeps the queue layout mounted while an empty publication becomes eligible", () => {
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
    expect(emptyState.closest(".live-radar-empty")?.parentElement).toBe(list);
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open Token Case" })).not.toBeInTheDocument();
    const evidenceClock = container.querySelector(".live-radar-header time");
    expect(evidenceClock).not.toBeNull();
    expect(evidenceClock).toHaveTextContent("no evidence");
    const emptySlot = container.querySelector(".live-radar-empty");
    const itemSlots = [...container.querySelectorAll(".live-radar-item")];
    expect(emptySlot).not.toBeNull();
    expect(emptySlot).toBeVisible();
    expect(emptySlot).not.toHaveAttribute("hidden");
    expect(itemSlots).toHaveLength(8);
    expect(itemSlots.every((slot) => !slot.hasAttribute("hidden"))).toBe(true);
    expect(itemSlots.every((slot) => slot.getAttribute("aria-hidden") === "true")).toBe(true);
    expect(itemSlots.every((slot) => slot.hasAttribute("inert"))).toBe(true);
    expect(
      itemSlots.every((slot) => slot.querySelector("a")?.getAttribute("tabindex") === "-1"),
    ).toBe(true);
    expect(itemSlots.map((slot) => getComputedStyle(slot).visibility)).toEqual(
      Array.from({ length: 8 }, () => "hidden"),
    );
    expect(itemSlots.map((slot) => getComputedStyle(slot).minHeight)).toEqual(
      Array.from({ length: 8 }, () => "75px"),
    );
    const slotChildren = itemSlots.map((slot) => [...slot.children]);
    expect(slotChildren.every((children) => children.length === 2)).toBe(true);
    const elementChanges: Element[] = [];
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of [...record.addedNodes, ...record.removedNodes]) {
          if (node instanceof Element) elementChanges.push(node);
        }
      }
    });
    observer.observe(list, { childList: true, subtree: true });

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
    expect(screen.getByText("No eligible cases")).not.toBeVisible();
    expect(container.querySelector(".live-radar-header time")).toBe(evidenceClock);
    expect(evidenceClock).not.toHaveTextContent("no evidence");
    elementChanges.push(
      ...observer
        .takeRecords()
        .flatMap((record) => [...record.addedNodes, ...record.removedNodes])
        .filter((node): node is Element => node instanceof Element),
    );
    observer.disconnect();
    expect(elementChanges).toEqual([]);
    expect(container.querySelector(".live-radar-empty")).toBe(emptySlot);
    expect(emptySlot).not.toBeVisible();
    expect(emptySlot).not.toHaveAttribute("hidden");
    expect([...container.querySelectorAll(".live-radar-item")]).toEqual(itemSlots);
    expect(itemSlots.map((slot) => [...slot.children])).toEqual(slotChildren);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "Open Token Case" })).toHaveLength(2);
    expect(container.querySelectorAll(".live-radar-item--empty")).toHaveLength(6);
    expect(itemSlots[0]).not.toHaveAttribute("aria-hidden");
    expect(itemSlots[0]).not.toHaveAttribute("inert");
    expect(itemSlots[2]).toHaveAttribute("aria-hidden", "true");
    expect(itemSlots[2]).toHaveAttribute("inert");
    expect(getComputedStyle(itemSlots[0]!).visibility).toBe("visible");
    expect(getComputedStyle(itemSlots[2]!).visibility).toBe("hidden");
    expect(container.querySelectorAll("*").length).toBeLessThanOrEqual(500);
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

function fixture(): TokenRadarSnapshot {
  return {
    schema_version: "token_radar_snapshot_v1",
    evidence_as_of_ms: 1_778_426_440_000,
    eligible_total: 8,
    items: [item("FIRST", "one", 5), item("SECOND", "two", 3)],
  };
}

function item(symbol: string, id: string, mentionDelta: number) {
  return {
    target: {
      target_type: "Asset" as const,
      target_id: `asset:solana:token:${id}`,
      symbol,
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
    market: { status: "confirmed" as const, price_change_since_signal: 0.12 },
    counter_evidence: null,
  };
}
