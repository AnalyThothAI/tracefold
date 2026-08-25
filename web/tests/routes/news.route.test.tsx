import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { newsStatusFixture } from "@tests/fixtures/newsFixture";
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
    // Enter submits: the box carries a `/` hint instead of a button, so the row spends its width on the
    // one input a reader actually uses.
    fireEvent.submit(search.closest("form")!);
    await waitFor(() => {
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/feed",
        expect.objectContaining({ params: expect.objectContaining({ q: "BTC ETF" }) }),
      );
    });
  });

  it.each([
    ["/news/status", "流水线状态", "/api/news/status"],
    ["/news/review", "学习复盘", "/api/news/review"],
    ["/news/oi", "持仓异动监控", "/api/news/status"],
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

  it("lights the topbar lamp with the failing item's own sentence when health degrades", async () => {
    // What the route level owns is the mapping: server `health` -> the frame's structural prop. That `ok`
    // draws nothing at all is the lamp's own rule and is pinned on the component in
    // `CockpitTopbarHealth.test.tsx`, so it is not rendered a second time here.
    setupAppRouteTest((mock) => {
      mockAppRoutes(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/news/status") return base(path, options);
        const health = newsStatusFixture().health;
        return {
          ok: true,
          data: newsStatusFixture({
            health: {
              ...health,
              model: { detail_zh: "p95 9.2 秒", level: "bad", summary_zh: "24 小时降级率 20%" },
              overall: "bad",
            },
          }),
        };
      };
    });
    renderAppRoute("/news/oi");

    // The mapping the shell owns: server level -> lamp level, worst item's `summary_zh` -> lamp text.
    const lamp = await screen.findByRole("button", { name: "流水线健康：24 小时降级率 20%" });
    expect(lamp).toHaveAttribute("data-level", "bad");
  });

  it("says the health read itself failed rather than going dark", async () => {
    /*
     * The lamp is the console's only health signal now, and the topbar's other indicator watches a
     * different endpoint (`/api/status`). Without this branch a console whose `/api/news/status` starts
     * failing shows nothing wrong anywhere — which is the shape of outage the lamp exists for.
     */
    setupAppRouteTest((mock) => {
      mockAppRoutes(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path === "/api/news/status") throw new Error("news status unavailable");
        return base(path, options);
      };
    });
    renderAppRoute("/news/oi");

    const lamp = await screen.findByRole("button", { name: "流水线健康：读取流水线状态失败" });
    expect(lamp).toHaveAttribute("data-level", "bad");
  });

  it("navigates between the three working surfaces with stable public URLs", async () => {
    renderAppRoute("/news");
    await screen.findByRole("heading", { name: "新闻事件流" });

    fireEvent.click(screen.getByRole("link", { name: "持仓异动" }));
    expect(await screen.findByRole("heading", { name: "持仓异动监控" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "学习复盘" }));
    expect(await screen.findByRole("heading", { name: "学习复盘" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "事件流" }));
    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();

    /*
     * At desktop width an Event opens *beside* the list, not instead of it (design proposal ⑦): the row link
     * is a real href — every modified click, middle click and assistive path still follows it — but a plain
     * click keeps the queue on screen and puts the Event in the drawer. 打开整页 is the way to the canonical,
     * shareable page from there.
     */
    fireEvent.click(await screen.findByRole("link", { name: /央行政策转向，风险资产承压/ }));
    const drawer = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/events/evt-global-policy",
        expect.any(Object),
      ),
    );
    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();

    fireEvent.click(within(drawer).getByRole("link", { name: "打开整页" }));
    expect(await screen.findByRole("region", { name: "新闻事件详情" })).toBeInTheDocument();
  });
});
