import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("news route", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    setupAppRouteTest(mockLiveRadarRoute);
  });

  it("routes topbar search into the News list query instead of global token search", async () => {
    renderAppRoute("/news");

    fireEvent.change(await screen.findByLabelText("news search"), {
      target: { value: "ethereum etf" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));

    await waitFor(() => {
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/stories",
        expect.objectContaining({
          params: expect.objectContaining({ q: "ethereum etf" }),
        }),
      );
    });
    expect(screen.queryByRole("heading", { name: "Search Intel" })).not.toBeInTheDocument();
    expect(
      apiMock.readApi.mock.calls.filter(([path]) => path === "/api/search/inspect"),
    ).toHaveLength(0);
    expect(screen.getByLabelText("Search stories")).toHaveValue("ethereum etf");
  }, 10_000);
});
