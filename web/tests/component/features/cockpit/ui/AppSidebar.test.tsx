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

  it("renders exactly the one supported primary destination", () => {
    renderSidebar();

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    expect(links.map((link) => [link.textContent?.trim(), link.getAttribute("href")])).toEqual([
      ["News", "/news"],
    ]);
  });

  it("marks News current for the feed route and keeps it current on a drilldown", () => {
    renderSidebar({ route: "/news/events/evt-1" });

    const newsLink = screen.getByRole("link", { name: "News" });
    expect(newsLink).toHaveAttribute("href", "/news");
    expect(newsLink).toHaveAttribute("aria-current", "page");
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
    expect(screen.getByRole("link", { name: "News" })).toBeInTheDocument();
    expect(document.querySelectorAll('[data-sidebar="menu-badge"]')).toHaveLength(0);
  });
});

function renderSidebar({
  route = "/",
}: {
  route?: string;
} = {}) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <SidebarProvider>
        <AppSidebar />
      </SidebarProvider>
    </MemoryRouter>,
  );
}
