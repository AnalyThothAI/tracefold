import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { mockAppRoutes } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("news route", () => {
  afterEach(cleanup);

  beforeEach(() => setupAppRouteTest(mockAppRoutes));

  it("uses the topbar as the sole search entry and synchronizes it with URL q", async () => {
    renderAppRoute("/news?q=bitcoin");

    await screen.findByRole("heading", { name: "新闻事件流" });
    const search = screen.getByRole("textbox", { name: "news search" });
    expect(search).toHaveValue("bitcoin");
    expect(screen.getAllByRole("textbox", { name: "news search" })).toHaveLength(1);
    await waitFor(() => {
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/feed",
        expect.objectContaining({ params: expect.objectContaining({ q: "bitcoin" }) }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "移除搜索：bitcoin" }));
    await waitFor(() => expect(search).toHaveValue(""));

    fireEvent.change(search, { target: { value: "BTC ETF" } });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await waitFor(() => {
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/feed",
        expect.objectContaining({ params: expect.objectContaining({ q: "BTC ETF" }) }),
      );
    });
  });

  it.each([
    ["/news/status", "流水线状态", "/api/news/status"],
    // #88: 命中复盘 is a hard-loadable destination of its own, on its own bounded endpoint.
    ["/news/review", "命中复盘", "/api/news/review"],
    [
      "/news/events/evt-global-policy",
      "央行政策转向，风险资产承压",
      "/api/news/events/evt-global-policy",
    ],
  ] as const)("hard-loads the public News view at %s", async (path, heading, endpoint) => {
    renderAppRoute(path);

    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    await waitFor(() => expect(apiMock.readApi).toHaveBeenCalledWith(endpoint, expect.any(Object)));
  });

  it("navigates between the Event Feed and Status with stable public URLs", async () => {
    renderAppRoute("/news");
    await screen.findByRole("heading", { name: "新闻事件流" });

    fireEvent.click(screen.getByRole("link", { name: "流水线状态" }));
    expect(await screen.findByRole("heading", { name: "流水线状态" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "命中复盘" }));
    expect(await screen.findByRole("heading", { name: "命中复盘" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "事件流" }));
    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("link", { name: /央行政策转向，风险资产承压/ }));
    expect(await screen.findByRole("region", { name: "新闻事件详情" })).toBeInTheDocument();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/events/evt-global-policy",
        expect.any(Object),
      ),
    );
  });
});
