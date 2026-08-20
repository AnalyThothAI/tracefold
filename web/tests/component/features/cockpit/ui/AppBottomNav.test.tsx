import { AppBottomNav } from "@features/cockpit/ui/AppBottomNav";
import { APP_NAVIGATION_GROUPS } from "@features/cockpit/ui/appNavigation";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

describe("AppBottomNav", () => {
  it("carries every destination the sidebar does, flattened", () => {
    renderBottomNav();

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const links = within(navigation).getAllByRole("link");
    // One model, two presentations (#87): a destination added to `appNavigation.ts` must reach the phone
    // without anyone remembering to add it here too.
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      APP_NAVIGATION_GROUPS.flatMap((group) => group.items).map((item) => item.to),
    );
    expect(links.map((link) => link.textContent?.trim())).toEqual([
      "事件流",
      "命中复盘",
      "流水线状态",
    ]);
  });

  it("marks the feed current on an Event drilldown, and only the feed", () => {
    renderBottomNav("/news/events/evt-1");

    // The same `isActive` predicate the sidebar uses, so the two presentations cannot disagree about where
    // the reader is. `NavLink` would decide by prefix and light up both.
    expect(screen.getByRole("link", { name: "事件流" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "流水线状态" })).not.toHaveAttribute("aria-current");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("marks status current on the status route", () => {
    renderBottomNav("/news/status");

    expect(screen.getByRole("link", { name: "流水线状态" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "事件流" })).not.toHaveAttribute("aria-current");
  });
});

function renderBottomNav(route = "/news") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppBottomNav />
    </MemoryRouter>,
  );
}
