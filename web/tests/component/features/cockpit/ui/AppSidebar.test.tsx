import { AppSidebar } from "@features/cockpit/ui/AppSidebar";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

describe("AppSidebar", () => {
  it("renders the Tracefold console mark and one focused navigation group", () => {
    renderSidebar();

    expect(screen.getByText("Tracefold")).toBeInTheDocument();
    expect(screen.getByText("News V3 Console")).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const headings = within(navigation).getAllByRole("heading", { level: 2 });
    expect(headings.map((heading) => heading.textContent?.trim())).toEqual(["Workbench"]);
  });

  it("renders the three supported primary destinations", () => {
    renderSidebar({ counts: { events: 1463, oiFrames: 188 } });

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    // #207: every slot is a working surface. 流水线状态 kept its route and lost its slot — a healthy
    // pipeline made it a click that answers "everything is fine".
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/news",
      "/news/oi",
      "/news/review",
    ]);
    // Both lanes carry their own 24 h intake, compacted to fit beside the label; review has no count.
    expect(links[0].textContent).toContain("事件流");
    expect(links[0].textContent).toContain("1.4k");
    expect(links[1].textContent).toContain("持仓异动");
    expect(links[1].textContent).toContain("188");
    expect(links[2].textContent?.trim()).toBe("学习复盘");
  });

  it("keeps the count out of the accessible name", () => {
    // It changes on a poll. Folding it into the link's name would rename the destination every few seconds;
    // the funnel and the OI telemetry band announce the same figures properly.
    renderSidebar({ counts: { events: 1463, oiFrames: 188 } });

    expect(screen.getByRole("link", { name: "事件流" })).toHaveAttribute("href", "/news");
    expect(screen.getByRole("link", { name: "持仓异动" })).toHaveAttribute("href", "/news/oi");
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

  it("marks only the OI monitor current on the OI route", () => {
    // `/news` is a prefix of `/news/oi`, so a link that decides for itself by prefix would leave two
    // destinations announcing themselves as the current page.
    renderSidebar({ route: "/news/oi" });

    expect(screen.getByRole("link", { name: "持仓异动" })).toHaveAttribute("aria-current", "page");
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
  counts?: { events?: number; oiFrames?: number };
  route?: string;
} = {}) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppSidebar counts={counts} />
    </MemoryRouter>,
  );
}
