import { AppSidebar } from "@features/cockpit/ui/AppSidebar";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

describe("AppSidebar", () => {
  it("renders the Tracefold console mark and one group of working surfaces", () => {
    renderSidebar();

    expect(screen.getByText("Tracefold")).toBeInTheDocument();
    expect(screen.getByText("News V3 Console")).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const headings = within(navigation).getAllByRole("heading", { level: 2 });
    /*
     * #256 split the list into a workbench and a data-health group; #553 PR-1 emptied the second one.
     * 市场事实 is a reading surface, not a frame-parse audit: it answers what the venues reported, and
     * whether the pipeline itself is telling the truth is the topbar lamp's question on every page.
     */
    expect(headings.map((heading) => heading.textContent?.trim())).toEqual(["Workbench"]);
    expect(screen.queryByText("System · 数据健康")).not.toBeInTheDocument();
  });

  it("renders the three supported primary destinations", () => {
    renderSidebar({ counts: { events: 1463 } });

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    // #207: every slot is a working surface. 流水线状态 kept its route and lost its slot — a healthy
    // pipeline made it a click that answers "everything is fine". #256 removed 学习复盘 outright: the
    // ReviewDesk is a CLI lane now. #460 removed Alpha 判定, whose Cases and frozen evidence are both
    // on 交易. #553 PR-1 renamed the telemetry audit to 市场事实 and moved it up beside the feed.
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/news",
      "/news/market",
      "/trading",
    ]);
    expect(links[0].textContent).toContain("事件流");
    expect(links[0].textContent).toContain("1.4k");
    /*
     * 市场事实 carries no count. `/api/news/status` reports the editorial funnel and nothing about market
     * intake since #553 PR-1, and the destination prints the per-kind figures itself the moment it opens.
     */
    expect(links[1].textContent?.trim()).toBe("市场事实");
    /*
     * 交易 carries neither a count nor a badge. At 204px a count clipped the label to one glyph
     * (#460), and the `tradingEnvironment` badge that replaced it cost every News route a 15 s poll of
     * `/api/trading/status` for a clock and the word `paper` the desk itself states (#537 PR-5).
     */
    expect(links[2].textContent?.trim()).toBe("交易");
  });

  it("no longer offers the retired ReviewDesk destination", () => {
    renderSidebar();

    expect(screen.queryByRole("link", { name: "学习复盘" })).not.toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    for (const link of within(navigation).getAllByRole("link")) {
      expect(link.getAttribute("href")).not.toBe("/news/review");
    }
  });

  it("names the trading destination and nothing about its configuration", () => {
    // The destination is 交易. It carried the execution environment as a badge until #537 PR-5, which
    // is a fact about the Runtime rather than about where the link goes.
    renderSidebar();

    expect(screen.getByRole("link", { name: "交易" })).toHaveAttribute("href", "/trading");
    expect(document.querySelector(".cockpit-app-sidebar-badge")).toBeNull();
  });

  it("keeps the count out of the accessible name", () => {
    // It changes on a poll. Folding it into the link's name would rename the destination every few seconds;
    // the feed's own labelled 24 h funnel announces the same figure properly.
    renderSidebar({ counts: { events: 1463 } });

    expect(screen.getByRole("link", { name: "事件流" })).toHaveAttribute("href", "/news");
    expect(screen.getByRole("link", { name: "市场事实" })).toHaveAttribute("href", "/news/market");
  });

  it("carries no health chrome of its own", () => {
    // #207 moved pipeline health to the topbar lamp: it reaches all three frames, where a sidebar dot only
    // ever reached the widest one, and it says *what* is wrong rather than only that something is.
    renderSidebar({ route: "/news" });

    expect(screen.queryByRole("link", { name: "流水线状态" })).not.toBeInTheDocument();
    expect(document.querySelector(".cockpit-app-sidebar-signal")).toBeNull();
  });

  it("marks the feed current on a drilldown, and only the feed", () => {
    renderSidebar({ route: "/news/events/evt-1" });

    const newsLink = screen.getByRole("link", { name: "事件流" });
    expect(newsLink).toHaveAttribute("href", "/news");
    expect(newsLink).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("marks only 市场事实 current on the market route", () => {
    // `/news` is a prefix of `/news/market`, so a link that decides for itself by prefix would leave two
    // destinations announcing themselves as the current page.
    renderSidebar({ route: "/news/market" });

    expect(screen.getByRole("link", { name: "市场事实" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "事件流" })).not.toHaveAttribute("aria-current");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("does not expose a retired Macro destination or persistent health chrome", () => {
    renderSidebar({ route: "/news" });

    for (const label of ["Macro", "宏观", "总览", "跨资产", "利率与通胀", "美联储"]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("status", { name: "Desk status" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Radar" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Stocks" })).not.toBeInTheDocument();
  });
});

function renderSidebar({
  counts,
  route = "/",
}: {
  counts?: { events?: number };
  route?: string;
} = {}) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppSidebar counts={counts} />
    </MemoryRouter>,
  );
}
