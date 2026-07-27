import type { WindowKey } from "@lib/types";
import type { TokenRadarVenueFilter } from "@lib/venue";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { tokenRadarFixture, tokenRadarRowFixture } from "@tests/fixtures/appRouteFixtures";
import { ok } from "@tests/msw/fixtures";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("live radar route", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  beforeEach(() => {
    setupAppRouteTest();
  });

  it("renders Token Radar as the default route", async () => {
    renderAppRoute("/");

    await screen.findByLabelText("token radar scan controls");
    expect(await screen.findByRole("heading", { name: "Token Radar" })).toBeInTheDocument();
    expect(await screen.findByText("Radar 正常 · 暂无内容")).toBeInTheDocument();
    expect(screen.queryByText(/实时信号 Tape/i)).not.toBeInTheDocument();
    expect(document.querySelector(".live-task-nav")).not.toBeInTheDocument();
    expect(apiMock.getApi.mock.calls.some(([path]) => path === "/api/recent")).toBe(false);
  });

  it("advances current-view content age without extra requests and resets on a newer watermark", async () => {
    const nowMs = 1_777_770_000_000;
    vi.useFakeTimers({
      now: nowMs,
      toFake: ["Date", "setInterval", "clearInterval"],
    });
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    const clearIntervalSpy = vi.spyOn(window, "clearInterval");
    let radarReads = 0;
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/token-radar") {
          return baseGetApi(path, options);
        }
        radarReads += 1;
        return ok(
          radarResponse({
            sourceMaxReceivedAtMs: radarReads === 1 ? nowMs - 10_000 : Date.now() - 1_000,
            withRow: true,
          }),
        );
      };
    });

    const rendered = renderAppRoute("/");

    expect(await screen.findByText("最新内容 10s")).toBeInTheDocument();
    const firstReadCount = radarRequestCount();
    expect(firstReadCount).toBe(1);
    const healthAnnouncement = screen
      .getAllByRole("status")
      .find((element) => element.textContent === "Radar 更新正常");
    expect(healthAnnouncement).toBeDefined();
    expect(healthAnnouncement).not.toHaveTextContent("10s");
    expect(screen.getByTestId("radar-content-status").tagName).toBe("DIV");

    act(() => {
      vi.advanceTimersByTime(2_000);
    });

    expect(screen.getByText("最新内容 12s")).toBeInTheDocument();
    expect(radarRequestCount()).toBe(firstReadCount);
    expect(
      screen.getAllByRole("status").some((element) => element.textContent === "Radar 更新正常"),
    ).toBe(true);

    act(() => {
      vi.advanceTimersByTime(8_000);
    });

    await waitFor(() => expect(radarRequestCount()).toBe(2));
    expect(await screen.findByText("最新内容 1s")).toBeInTheDocument();

    const statusTimerIndex = setIntervalSpy.mock.calls.findIndex(([, delay]) => delay === 1_000);
    const statusTimer = setIntervalSpy.mock.results[statusTimerIndex]?.value;
    expect(statusTimer).toBeDefined();
    rendered.unmount();
    expect(clearIntervalSpy).toHaveBeenCalledWith(statusTimer);
    setIntervalSpy.mockRestore();
    clearIntervalSpy.mockRestore();
  });

  it("keeps placeholder freshness neutral for every query identity dimension", async () => {
    let blockNextRadarRead = false;
    let releaseRadarRead: (() => void) | null = null;
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/token-radar") {
          return baseGetApi(path, options);
        }
        if (blockNextRadarRead) {
          blockNextRadarRead = false;
          await new Promise<void>((resolve) => {
            releaseRadarRead = resolve;
          });
        }
        return ok(
          radarResponse({
            identity: radarIdentityFromOptions(options),
            sourceMaxReceivedAtMs: Date.now() - 3_000,
            withRow: true,
          }),
        );
      };
    });
    renderAppRoute("/");
    expect(await screen.findByText("最新内容 3s")).toBeInTheDocument();

    for (const control of [
      () => screen.getByRole("button", { name: "SOL" }),
      () => within(screen.getByLabelText("radar window")).getByRole("radio", { name: "4h" }),
    ]) {
      blockNextRadarRead = true;
      releaseRadarRead = null;
      fireEvent.click(control());
      await waitFor(() => expect(releaseRadarRead).not.toBeNull());
      expect(screen.getByText("正在读取")).toBeInTheDocument();
      expect(screen.queryByText("最新内容 3s")).not.toBeInTheDocument();
      act(() => releaseRadarRead?.());
      expect(await screen.findByText("最新内容 3s")).toBeInTheDocument();
    }
  });

  it("preserves last-good rows through refresh failure, timeout, stale projection, and recovery", async () => {
    const nowMs = 1_777_770_000_000;
    vi.useFakeTimers({
      now: nowMs,
      toFake: ["Date", "setInterval", "clearInterval"],
    });
    let mode: "fresh" | "error" | "stale" = "fresh";
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/token-radar") {
          return baseGetApi(path, options);
        }
        if (mode === "error") {
          throw new Error("controlled refresh failure");
        }
        return ok(
          radarResponse({
            projectionStatus: mode,
            sourceMaxReceivedAtMs: Date.now() - 5_000,
            withRow: true,
          }),
        );
      };
    });
    renderAppRoute("/");

    expect(await screen.findByText("最新内容 5s")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Token Radar item $UPEG" })).toBeInTheDocument();

    mode = "error";
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(await screen.findByText("刷新延迟 · 内容 15s")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Token Radar item $UPEG" })).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(21_000);
    });
    expect(screen.getByTestId("radar-content-status")).toHaveTextContent("Radar 不可用");
    expect(screen.getByRole("article", { name: "Token Radar item $UPEG" })).toBeInTheDocument();

    mode = "fresh";
    act(() => {
      vi.advanceTimersByTime(9_000);
    });
    expect(await screen.findByText("最新内容 5s")).toBeInTheDocument();

    mode = "stale";
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(await screen.findByText(/刷新延迟 · 内容/)).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Token Radar item $UPEG" })).toBeInTheDocument();
  });

  it("fails closed on an initial read or projection failure and clamps future watermarks", async () => {
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path === "/api/token-radar") {
          throw new Error("initial radar failure");
        }
        return baseGetApi(path, options);
      };
    });
    const first = renderAppRoute("/");
    await waitFor(() =>
      expect(screen.getByTestId("radar-content-status")).toHaveTextContent("Radar 不可用"),
    );
    expect(screen.getByText(/initial radar failure/)).toBeInTheDocument();
    first.unmount();

    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/token-radar") {
          return baseGetApi(path, options);
        }
        return ok(
          radarResponse({
            projectionStatus: "failed",
            sourceMaxReceivedAtMs: 0,
          }),
        );
      };
    });
    const second = renderAppRoute("/");
    await waitFor(() =>
      expect(screen.getByTestId("radar-content-status")).toHaveTextContent("Radar 不可用"),
    );
    second.unmount();

    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/token-radar") {
          return baseGetApi(path, options);
        }
        return ok(
          radarResponse({
            sourceMaxReceivedAtMs: Date.now() + 60_000,
            withRow: true,
          }),
        );
      };
    });
    renderAppRoute("/");
    expect(await screen.findByText("最新内容 0s")).toBeInTheDocument();
  });

  it("keeps the chain selector to the left of the radar window controls", async () => {
    renderAppRoute("/");

    const controls = await screen.findByLabelText("token radar scan controls");
    const chainLabels = within(controls)
      .getAllByRole("button")
      .map((button) => button.textContent);
    const windowGroup = within(controls).getByLabelText("radar window");
    const windowLabels = within(windowGroup)
      .getAllByRole("radio")
      .map((radio) => radio.textContent);

    expect(chainLabels.slice(0, 6)).toEqual(["All", "SOL", "ETH", "BASE", "BSC", "CEX"]);
    expect(windowLabels).toEqual(["5m", "1h", "4h", "24h"]);
    expect(
      within(controls).getByRole("button", { name: "CEX" }).compareDocumentPosition(windowGroup) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("keeps primary navigation free of server-backed badges", async () => {
    renderAppRoute("/");

    const navigation = await screen.findByRole("navigation", { name: "Primary navigation" });

    expect(within(navigation).getByRole("link", { name: /^Radar$/i })).toBeInTheDocument();
    expect(within(navigation).getByRole("link", { name: /Stocks/i })).toBeInTheDocument();
    expect(within(navigation).getByRole("link", { name: /News/i })).toBeInTheDocument();
    expect(within(navigation).queryByText("2")).not.toBeInTheDocument();
    expect(within(navigation).queryByText("2+")).not.toBeInTheDocument();
  });

  it("treats pending projection coverage as loading instead of empty data", async () => {
    setupAppRouteTest((apiMock) => {
      mockLiveRadarRoute(apiMock);
      const baseGetApi = apiMock.getApiImpl;
      apiMock.getApiImpl = async (path, options) => {
        if (path === "/api/token-radar") {
          return {
            ok: true,
            data: {
              window: "1h",
              venue: "all",
              targets: [],
              attention: [],
              projection: {
                status: "pending",
                version: "token-radar-route-fixture",
                source: "token_radar_current_rows",
                venue: "all",
                reason: "projection_window_running",
                latest_attempt_status: "running",
                row_count: 0,
                source_rows: 3,
                source_max_received_at_ms: 0,
                source_frontier_ms: null,
                computed_at_ms: null,
                error: null,
                anchor_coverage: { status: "pending", ready: 0, missing: 0, total: 0 },
                quality_status: "insufficient",
                degraded_reasons: ["projection_window_running"],
                unresolved: {
                  identity_missing_count: 0,
                  nil_count: 0,
                  ambiguous_count: 0,
                  sample_symbols: [],
                },
              },
            },
          };
        }
        return baseGetApi(path, options);
      };
    });
    renderAppRoute("/");

    await screen.findByLabelText("token radar scan controls");
    expect(await screen.findByLabelText("loading token radar")).toBeInTheDocument();
    expect(screen.queryByText("当前窗口暂无可交易 token 热度")).not.toBeInTheDocument();
  });
});

function radarRequestCount(): number {
  return apiMock.getApi.mock.calls.filter(([path]) => path === "/api/token-radar").length;
}

function radarResponse({
  identity = { venue: "all", window: "1h" },
  projectionStatus = "fresh",
  sourceMaxReceivedAtMs,
  withRow = false,
}: {
  identity?: {
    venue: TokenRadarVenueFilter;
    window: WindowKey;
  };
  projectionStatus?: string;
  sourceMaxReceivedAtMs: number;
  withRow?: boolean;
}) {
  const base = tokenRadarFixture();
  const rows = withRow ? [tokenRadarRowFixture()] : [];
  return {
    ...base,
    targets: rows,
    venue: identity.venue,
    window: identity.window,
    projection: {
      ...base.projection,
      error: projectionStatus === "failed" ? "controlled projection failure" : null,
      latest_attempt_status: projectionStatus === "failed" ? "failed" : "ready",
      reason: projectionStatus === "fresh" ? null : `controlled_${projectionStatus}`,
      row_count: rows.length,
      source_max_received_at_ms: sourceMaxReceivedAtMs,
      source_rows: rows.length,
      status: projectionStatus,
      venue: identity.venue,
    },
  };
}

function radarIdentityFromOptions(
  options:
    | {
        params?: Record<string, string | number | boolean | null | undefined>;
      }
    | undefined,
): {
  venue: TokenRadarVenueFilter;
  window: WindowKey;
} {
  return {
    venue: String(options?.params?.venue ?? "all") as TokenRadarVenueFilter,
    window: String(options?.params?.window ?? "1h") as WindowKey,
  };
}
