import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { tokenRadarFixture } from "@tests/fixtures/appRouteFixtures";
import { ok } from "@tests/msw/fixtures";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("live radar route", () => {
  afterEach(cleanup);
  beforeEach(() => setupAppRouteTest());

  it("renders the rich server-prioritized queue without Radar URL controls", async () => {
    renderAppRoute("/?window=4h&venue=sol&sort=score");

    expect(await screen.findByRole("heading", { name: "Radar" })).toBeInTheDocument();
    expect(await screen.findByText("1 eligible · showing 1 / 50")).toBeInTheDocument();
    expect(screen.getByText("4h causal change · newest qualification first")).toBeInTheDocument();
    expect(await screen.findByText(/\$UPEG/)).toBeInTheDocument();
    expect(screen.getByText(/\+5/)).toBeInTheDocument();
    expect(screen.queryByLabelText("radar window")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("token radar venue filter")).not.toBeInTheDocument();
    expect(screen.queryByText(/score/i)).not.toBeInTheDocument();

    expect(screen.getByRole("link", { name: "Open Token Case" })).toHaveAttribute(
      "href",
      expect.stringContaining("?window=4h&focus=trigger&trigger_event_id=event-upeg-1"),
    );
    const radarRequest = apiMock.getApi.mock.calls.find(([path]) => path === "/api/token-radar");
    expect(radarRequest?.[1]?.params).toBeUndefined();
  });

  it("retains the last good queue and reports a delayed refresh", async () => {
    let radarReads = 0;
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/token-radar") return base(path, options);
        radarReads += 1;
        if (radarReads > 1) throw new Error("controlled refresh failure");
        return ok(tokenRadarFixture());
      };
    });
    renderAppRoute("/");
    expect(await screen.findByText(/\$UPEG/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByText("更新延迟")).toHaveAttribute("role", "status");
    expect(screen.getByText(/\$UPEG/)).toBeInTheDocument();
  });

  it("renders server-owned stale state while retaining its last-known-good row", async () => {
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) =>
        path === "/api/token-radar"
          ? ok(
              tokenRadarFixture({
                state: "stale",
                stale_reason: "projection_failed",
              }),
            )
          : base(path, options);
    });
    renderAppRoute("/");

    expect(await screen.findByText(/Radar stale/)).toHaveTextContent("Projection unavailable");
    expect(screen.getByText(/\$UPEG/)).toBeInTheDocument();
  });

  it("renders server-owned unavailable state without a placeholder queue row", async () => {
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) =>
        path === "/api/token-radar"
          ? ok(
              tokenRadarFixture({
                state: "unavailable",
                stale_reason: null,
                state_changed_at_ms: 0,
                social_evidence_as_of_ms: 0,
                eligible_total: 0,
                items: [],
              }),
            )
          : base(path, options);
    });
    renderAppRoute("/");

    expect(await screen.findByText("Radar unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No eligible cases")).not.toBeInTheDocument();
  });

  it("renders an empty no-evidence snapshot without a 1970 timestamp", async () => {
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) =>
        path === "/api/token-radar"
          ? ok(tokenRadarFixture({ social_evidence_as_of_ms: 0, eligible_total: 0, items: [] }))
          : base(path, options);
    });
    renderAppRoute("/");

    expect(await screen.findByText("No social evidence yet")).toBeInTheDocument();
    expect(screen.getByText("No eligible cases")).toBeInTheDocument();
    expect(screen.queryByText(/1970/)).not.toBeInTheDocument();
  });

  it("ends the loading state when the read-session bootstrap fails", async () => {
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      mock.getBootstrapImpl = async () => {
        throw new Error("controlled bootstrap failure");
      };
    });
    renderAppRoute("/");

    expect(
      await screen.findByText("Radar read session could not be established."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("loading Radar")).not.toBeInTheDocument();
    expect(apiMock.getApi.mock.calls.some(([path]) => path === "/api/token-radar")).toBe(false);
  });

  it("reports an unavailable read session when bootstrap returns no token", async () => {
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      mock.getBootstrapImpl = async () => ok({ ws_token: "", replay_limit: 25 });
    });
    renderAppRoute("/");

    expect(await screen.findByText("Radar read session unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.queryByLabelText("loading Radar")).not.toBeInTheDocument();
    expect(apiMock.getApi.mock.calls.some(([path]) => path === "/api/token-radar")).toBe(false);
  });

  it("shows session loading only while bootstrap is pending", async () => {
    setupAppRouteTest((mock) => {
      mockLiveRadarRoute(mock);
      mock.getBootstrapImpl = () => new Promise<never>(() => undefined);
    });
    renderAppRoute("/");

    expect(
      await screen.findByRole("status", { name: "establishing Radar read session" }),
    ).toBeInTheDocument();
    expect(apiMock.getApi.mock.calls.some(([path]) => path === "/api/token-radar")).toBe(false);
  });

  it("keeps primary navigation free of server-backed badges", async () => {
    renderAppRoute("/");
    const navigation = await screen.findByRole("navigation", { name: "Primary navigation" });

    expect(within(navigation).getByRole("link", { name: /^Radar$/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/\$UPEG/)).toBeInTheDocument());
    expect(within(navigation).queryByText("1")).not.toBeInTheDocument();
  });
});
