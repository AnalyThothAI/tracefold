import { AppSidebar } from "@features/cockpit/ui/AppSidebar";
import { SidebarProvider } from "@shared/ui/sidebar";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

describe("AppSidebar", () => {
  it("renders the Tracefold research workbench and one focused navigation group", () => {
    renderSidebar();

    expect(screen.getByText("Tracefold")).toBeInTheDocument();
    expect(screen.getByText("Research Workbench")).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const headings = within(navigation).getAllByRole("heading", { level: 2 });
    expect(headings.map((heading) => heading.textContent?.trim())).toEqual(["Research"]);
  });

  it("renders the three supported primary destinations", () => {
    renderSidebar({ counts: { events: 1463 } });

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/news",
      "/news/review",
      "/news/status",
    ]);
    // The feed carries the 24 h intake behind it; review and status have no count of their own.
    expect(links[0].textContent).toContain("事件流");
    expect(links[0].textContent).toContain("1,463");
    expect(links[1].textContent?.trim()).toBe("命中复盘");
    expect(links[2].textContent?.trim()).toBe("流水线状态");
  });

  it("marks the feed current on a drilldown, and only the feed", () => {
    renderSidebar({ route: "/news/events/evt-1" });

    const newsLink = screen.getByRole("link", { name: "事件流" });
    expect(newsLink).toHaveAttribute("href", "/news");
    expect(newsLink).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("marks only the status route current on the status route", () => {
    // `/news` is a prefix of `/news/status`, so a link that decides for itself by prefix would leave two
    // destinations announcing themselves as the current page.
    renderSidebar({ route: "/news/status" });

    expect(screen.getByRole("link", { name: "流水线状态" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "事件流" })).not.toHaveAttribute("aria-current");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("does not expose a retired Macro destination or persistent health chrome", () => {
    renderSidebar({ route: "/news" });

    for (const label of ["Macro", "宏观", "总览", "跨资产", "利率与通胀", "美联储"]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("status", { name: "Desk status" })).not.toBeInTheDocument();
  });

  it("keeps navigation free of server-backed badges", () => {
    renderSidebar();

    expect(screen.queryByRole("link", { name: "Radar" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Stocks" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "事件流" })).toBeInTheDocument();
    expect(document.querySelectorAll('[data-sidebar="menu-badge"]')).toHaveLength(0);
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
      <SidebarProvider>
        <AppSidebar counts={counts} />
      </SidebarProvider>
    </MemoryRouter>,
  );
}
