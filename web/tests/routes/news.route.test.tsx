import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("news route", () => {
  afterEach(cleanup);

  beforeEach(() => setupAppRouteTest(mockLiveRadarRoute));

  it("uses the topbar as the sole search entry and synchronizes it with URL q", async () => {
    renderAppRoute("/news?q=bitcoin");

    await screen.findByRole("heading", { name: "全球新闻" });
    const search = screen.getByRole("textbox", { name: "news search" });
    expect(search).toHaveValue("bitcoin");
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
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
    ["/news/status", "新闻运行状态", "/api/news/status"],
    ["/news/sources", "公开新闻来源", "/api/news/sources"],
  ] as const)("hard-loads the public News view at %s", async (path, heading, endpoint) => {
    renderAppRoute(path);

    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    await waitFor(() => expect(apiMock.readApi).toHaveBeenCalledWith(endpoint, expect.any(Object)));
  });

  it("navigates among Feed, Brief, Status, and Sources with stable public URLs", async () => {
    renderAppRoute("/news");
    await screen.findByRole("heading", { name: "全球新闻" });

    fireEvent.click(screen.getByRole("link", { name: "状态" }));
    expect(await screen.findByRole("heading", { name: "新闻运行状态" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "来源" }));
    expect(await screen.findByRole("heading", { name: "公开新闻来源" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "公共全球简报" }));
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith("/api/news/brief", expect.any(Object)),
    );
  });
});
