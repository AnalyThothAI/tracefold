import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { newsEventDetailFixture, newsStatusFixture } from "@tests/fixtures/newsFixture";
import { mockAppRoutes } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { axe } from "jest-axe";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("news route", () => {
  afterEach(cleanup);

  beforeEach(() => setupAppRouteTest(mockAppRoutes));

  it("shows current adopted sources without a legacy evidence projection", async () => {
    renderAppRoute("/news/events/evt-global-policy?tab=source");
    expect(await screen.findByRole("region", { name: "来源与分歧" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "来源证据" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByRole("tabpanel", { name: "来源证据" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "本次判断的证据" })).toBeNull();
    expect(apiMock.readApi).toHaveBeenCalledWith(
      "/api/news/events/evt-global-policy",
      expect.any(Object),
    );
  });

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

    fireEvent.change(search, { target: { value: "" } });
    fireEvent.submit(search.closest("form")!);
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

  it("has no automated accessibility violations on the complete successful route", async () => {
    const { container } = renderAppRoute("/news");

    await screen.findByRole("link", { name: /央行政策转向，风险资产承压/ });
    await screen.findByRole("button", { name: /流水线健康/ });
    expect(screen.queryByText("正在读取新闻事件")).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it.each([
    ["/news/status", "流水线状态", "/api/news/status"],
    ["/news/market", "市场研究", "/api/news/market"],
    ["/news/wallets", "聪明钱警报", "/api/news/wallets/events"],
    [
      "/news/events/evt-global-policy",
      "钢铁进口关税上调至 50%",
      "/api/news/events/evt-global-policy",
    ],
  ] as const)("hard-loads the public News view at %s", async (path, heading, endpoint) => {
    renderAppRoute(path);

    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    await waitFor(() => expect(apiMock.readApi).toHaveBeenCalledWith(endpoint, expect.any(Object)));
  });

  it("lights the topbar lamp with the failing item's own sentence when health degrades", async () => {
    // What the route level owns is the mapping: server `health` -> the frame's structural prop. The lamp
    // remains the status-page door while healthy; this case verifies that degradation also carries the
    // failing stage's sentence rather than merely changing colour.
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
            instruments: { ...newsStatusFixture().instruments!, last_snapshot_ms: null },
          }),
        };
      };
    });
    renderAppRoute("/news/market");

    // The mapping the shell owns: server level -> lamp level, worst item's `summary_zh` -> lamp text.
    const lamp = await screen.findByRole("button", { name: "流水线健康：24 小时降级率 20%" });
    expect(lamp).toHaveAttribute("data-level", "bad");
    fireEvent.click(lamp);
    expect(
      within(await screen.findByRole("dialog")).queryByText("标的表", { exact: true }),
    ).not.toBeInTheDocument();
  });

  it("draws the frame's in-flight line only while the frame's own reads are pending", async () => {
    let releaseStatus: (() => void) | null = null;
    const held = new Promise<void>((resolve) => {
      releaseStatus = resolve;
    });
    setupAppRouteTest((mock) => {
      mockAppRoutes(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path === "/api/news/status") await held;
        return base(path, options);
      };
    });
    const { container } = renderAppRoute("/news");

    await waitFor(() =>
      expect(container.querySelector(".page-state-route-progress")).not.toBeNull(),
    );

    releaseStatus!();
    // A poll is not a cold start: the line must not come back every few seconds once the frame has its
    // answers, or it stops meaning "still loading".
    await waitFor(() => expect(container.querySelector(".page-state-route-progress")).toBeNull());
  });

  it("keeps the pipeline door on a secondary route while the pipeline is healthy", async () => {
    /*
     * 流水线状态 holds no navigation slot (#207). Hiding the lamp on the healthy path would leave the page
     * unreachable exactly when a reader wants to confirm nothing is wrong, so #256 keeps the affordance on
     * every route and lets its level carry the news instead.
     */
    setupAppRouteTest(mockAppRoutes);
    renderAppRoute("/news/events/evt-global-policy");

    const lamp = await screen.findByRole("button", { name: /流水线健康/ });
    expect(lamp).toHaveAttribute("data-level", "ok");
    expect(lamp).toHaveTextContent("流水线");
    fireEvent.click(lamp);
    expect(
      within(await screen.findByRole("dialog")).getByRole("link", { name: /打开流水线状态/ }),
    ).toHaveAttribute("href", "/news/status");
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
    renderAppRoute("/news/market");

    const lamp = await screen.findByRole("button", { name: "流水线健康：读取流水线状态失败" });
    expect(lamp).toHaveAttribute("data-level", "bad");
  });

  it("navigates between the three working surfaces with stable public URLs", async () => {
    setupAppRouteTest((mock) => {
      mockAppRoutes(mock);
      const base = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path !== "/api/news/events/evt-global-policy") return base(path, options);
        const detail = newsEventDetailFixture();
        return {
          ok: true,
          data: {
            ...detail,
            event: {
              ...detail.event,
              assets: [
                {
                  base_symbol: "SKHY",
                  market_type: "equity" as const,
                  resolution_state: "resolved" as const,
                  listed: true,
                  symbol: "SKHX",
                  venue: "hl.xyz",
                  venue_symbol: "xyz:SKHY",
                },
              ],
              grounded_assets: ["BTC", "SKHX"],
            },
            event_update: {
              ...detail.event_update!,
              claims: detail.event_update!.claims.map((claim, index) =>
                index === 0
                  ? {
                      ...claim,
                      assets: [{ role: "primary", symbol: "SKHX", market_type: "equity" }],
                    }
                  : claim,
              ),
            },
          },
        };
      };
    });
    renderAppRoute("/news");
    await screen.findByRole("heading", { name: "新闻事件流" });

    fireEvent.click(screen.getByRole("link", { name: "市场研究" }));
    expect(await screen.findByRole("heading", { name: "市场研究" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "事件流" }));
    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();

    const headline = await screen.findByRole("link", { name: /央行政策转向，风险资产承压/ });
    expect(headline).toHaveAttribute("href", "/news/events/evt-global-policy");
    fireEvent.click(headline);
    const detailPage = await screen.findByRole("region", { name: "新闻事件详情" });
    const detailHeadline = await within(detailPage).findByRole("heading", {
      name: "钢铁进口关税上调至 50%",
    });
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/events/evt-global-policy",
        expect.any(Object),
      ),
    );
    expect(screen.queryByRole("heading", { name: "新闻事件流" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      within(detailHeadline.closest("article")!).getByRole("link", {
        name: "SKHX",
      }),
    ).toHaveAttribute("href", "/news/symbols/SKHY");
  });

  it("returns directly from an Event to the same feed filters", async () => {
    const feedPath = "/news?q=tariff&outcome=held&hours=168&event_kind=news";
    renderAppRoute(feedPath);
    fireEvent.click(await screen.findByRole("link", { name: /央行政策转向，风险资产承压/ }));
    await screen.findByRole("heading", { name: "钢铁进口关税上调至 50%" });

    const back = screen.getByRole("link", { name: "返回新闻事件流" });
    expect(back).toHaveAttribute("href", feedPath);
    fireEvent.click(back);

    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "news search" })).toHaveValue("tariff");
    expect(screen.getByRole("tab", { name: "被拦截 271" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("button", { name: "时间范围，最近 7 天" })).toBeInTheDocument();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/news/feed",
        expect.objectContaining({
          params: expect.objectContaining({
            q: "tariff",
            outcome: "held",
            hours: "168",
            event_kind: "news",
          }),
        }),
      ),
    );
  });

  it("reaches the token page from an asset chip on a hard-refreshable URL", async () => {
    /*
     * #207 principle 9. The chip is the entry point every surface shares, and `/news/symbols/:base` is a
     * real route rather than in-page state — a reader who lands on it by refresh or by a shared link gets
     * the page, not the SPA's 404.
     */
    renderAppRoute("/news");
    await screen.findByRole("heading", { name: "新闻事件流" });

    fireEvent.click((await screen.findAllByRole("link", { name: "BTC" }))[0]);

    expect(await screen.findByRole("region", { name: "代币 BTC" })).toBeInTheDocument();

    cleanup();
    renderAppRoute("/news/symbols/BTC");
    expect(await screen.findByRole("region", { name: "代币 BTC" })).toBeInTheDocument();
  });

  it("sends the reader back to the page they actually left, with its filters", async () => {
    /*
     * Four surfaces link to a token page (#256). A back control that always said 事件流 named a page the
     * reader had never been on three times out of four, and from the feed it dropped the filters they
     * arrived with — the referrer travels as route state precisely so neither can happen.
     *
     * The feed is the entrance exercised here because it is the one whose *query* also has to survive; the
     * label each other entrance contributes is `routeReferrer`'s own mapping, unit-tested beside it.
     */
    renderAppRoute("/news?outcome=held&hours=168");
    await screen.findByRole("heading", { name: "新闻事件流" });

    fireEvent.click((await screen.findAllByRole("link", { name: "BTC" }))[0]);
    await screen.findByRole("region", { name: "代币 BTC" });
    expect(screen.getByRole("link", { name: "返回事件流" })).toHaveAttribute(
      "href",
      "/news?outcome=held&hours=168",
    );

    cleanup();
    // A cold URL carries no referrer and must not invent one.
    renderAppRoute("/news/symbols/BTC");
    await screen.findByRole("region", { name: "代币 BTC" });
    expect(screen.getByRole("link", { name: "返回事件流" })).toHaveAttribute("href", "/news");
  });
});
