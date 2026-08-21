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
    renderSidebar({ counts: { events: 1463 } });

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/news",
      "/news/review",
      "/news/status",
    ]);
    // The feed carries the 24 h intake behind it, compacted to fit beside the label; review and status have
    // no count of their own.
    expect(links[0].textContent).toContain("事件流");
    expect(links[0].textContent).toContain("1.4k");
    expect(links[1].textContent?.trim()).toBe("命中复盘");
    expect(links[2].textContent?.trim()).toBe("流水线状态");
  });

  it("keeps the count and the health dot out of the accessible name", () => {
    // Both change on a poll. Folding either into the link's name would rename the destination every few
    // seconds; the funnel and the status cards announce the same figures properly.
    renderSidebar({ counts: { events: 1463 }, statusLevel: "warn" });

    expect(screen.getByRole("link", { name: "事件流" })).toHaveAttribute("href", "/news");
    expect(screen.getByRole("link", { name: "流水线状态" })).toHaveAttribute(
      "href",
      "/news/status",
    );
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
    expect(screen.queryByRole("link", { name: "Radar" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Stocks" })).not.toBeInTheDocument();
  });
});

function renderSidebar({
  counts,
  route = "/",
  statusLevel,
}: {
  counts?: { events?: number };
  route?: string;
  statusLevel?: "ok" | "warn" | "bad";
} = {}) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppSidebar counts={counts} statusLevel={statusLevel} />
    </MemoryRouter>,
  );
}
