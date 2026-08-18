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

  it("renders exactly the two supported primary destinations in task order", () => {
    renderSidebar();

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    expect(links.map((link) => [link.textContent?.trim(), link.getAttribute("href")])).toEqual([
      ["News", "/news"],
      ["Macro", "/macro"],
    ]);
  });

  it("marks Macro current for the overview route", () => {
    renderSidebar({ route: "/macro" });

    const macroLink = screen.getByRole("link", { name: "Macro" });
    expect(macroLink).toHaveAttribute("href", "/macro");
    expect(macroLink).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("keeps the single Macro destination current on a drilldown", () => {
    renderSidebar({ route: "/macro?session_date=2026-07-22" });

    expect(screen.getByRole("link", { name: "Macro" })).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("does not expose nested Macro navigation or persistent health chrome", () => {
    renderSidebar({ route: "/macro" });

    for (const label of [
      "总览",
      "跨资产",
      "利率与通胀",
      "增长与就业",
      "流动性与资金",
      "信用",
      "大类资产",
      "利率",
      "流动性",
      "经济数据",
      "波动率",
      "美股",
      "收益率曲线",
      "相关性",
      "美联储",
      "Dashboard",
      "CDS 代理",
    ]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: /宏观/ })).not.toBeInTheDocument();
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
