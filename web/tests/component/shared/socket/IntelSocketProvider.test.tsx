import type { ApiResponse, LiveMarketUpdatePayload, TokenCaseDossier } from "@lib/types";
import { queryKeys } from "@shared/query/queryKeys";
import { IntelSocketProvider } from "@shared/socket/IntelSocketProvider";
import { normalizeMarketTargets } from "@shared/socket/marketTargets";
import { useSocketSnapshot } from "@shared/socket/socketContext";
import { useMarketSubscription } from "@shared/socket/useMarketSubscription";
import { act, cleanup, waitFor } from "@testing-library/react";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { afterEach, describe, expect, it, vi } from "vitest";

const websocketHarness = vi.hoisted(() => ({
  instances: [] as Array<{
    close: ReturnType<typeof vi.fn>;
    emit: (type: string, event?: unknown) => void;
    send: ReturnType<typeof vi.fn>;
  }>,
}));

vi.mock("reconnecting-websocket", () => {
  class MockReconnectingWebSocket {
    readonly close = vi.fn();
    readonly send = vi.fn();
    private readonly listeners = new Map<string, Array<(event: unknown) => void>>();

    constructor() {
      websocketHarness.instances.push(this);
    }

    addEventListener(type: string, listener: (event: unknown) => void) {
      const listeners = this.listeners.get(type) ?? [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    }

    emit(type: string, event: unknown = {}) {
      for (const listener of this.listeners.get(type) ?? []) listener(event);
    }
  }
  return { default: MockReconnectingWebSocket };
});

afterEach(() => {
  cleanup();
  websocketHarness.instances.length = 0;
});

describe("IntelSocketProvider", () => {
  it("normalizes market targets deterministically", () => {
    expect(
      normalizeMarketTargets([
        { target_type: "Asset", target_id: "b" },
        { target_type: "Asset", target_id: "a" },
        { target_type: "Asset", target_id: "a" },
        { target_type: "", target_id: "missing" },
        { target_type: "Asset", target_id: null },
      ]),
    ).toEqual([
      { target_type: "Asset", target_id: "a" },
      { target_type: "Asset", target_id: "b" },
    ]);
  });

  it("subscribes to an explicit Case target and patches only the Token Case cache", async () => {
    const dossier = tokenCaseFixture();
    const target = {
      target_id: dossier.target.target_id!,
      target_type: dossier.target.target_type!,
    };
    const view = renderWithProviders(
      <IntelSocketProvider token="secret">
        <SocketProbe target={target} />
      </IntelSocketProvider>,
    );
    const caseKey = queryKeys.tokenCase(`${target.target_type}:${target.target_id}`, "1h", 24);
    view.queryClient.setQueryData<ApiResponse<TokenCaseDossier>>(caseKey, {
      ok: true,
      data: dossier,
    });
    const socket = websocketHarness.instances[0];

    act(() => {
      socket.emit("open");
      socket.emit("message", { data: JSON.stringify({ type: "ready" }) });
    });
    await waitFor(() => {
      expect(sentMessages(socket)).toContainEqual({
        type: "subscribe",
        market_targets: [target],
        replay: 0,
      });
    });

    const update: LiveMarketUpdatePayload = {
      type: "live_market_update",
      target_id: target.target_id,
      target_type: target.target_type,
      market: {
        decision_latest: {
          target_id: target.target_id,
          target_type: target.target_type,
          source: "decision_latest",
          provider: "test",
          price_usd: 42,
          price_basis: "usd",
          observed_at_ms: 2,
          received_at_ms: 2,
        },
      },
    };
    act(() => socket.emit("message", { data: JSON.stringify(update) }));

    expect(
      view.queryClient.getQueryData<ApiResponse<TokenCaseDossier>>(caseKey)?.data.market_live
        .price_usd,
    ).toBe(42);
  });
});

function SocketProbe({ target }: { target: { target_id: string; target_type: string } }) {
  useMarketSubscription([target]);
  const snapshot = useSocketSnapshot();
  return <span data-testid="socket-snapshot-keys">{Object.keys(snapshot).sort().join(",")}</span>;
}

function sentMessages(socket: { send: ReturnType<typeof vi.fn> }) {
  return socket.send.mock.calls.map(([message]) => JSON.parse(String(message)) as unknown);
}
