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
      "持仓异动",
      // The phone bar shows the label alone: the sidebar's PAPER chip is 8px of monospace and a 48px thumb
      // target has no room for it. The page states the mode in a labelled figure either way.
      "交易",
      "学习复盘",
    ]);
  });

  it("marks the feed current on an Event drilldown, and only the feed", () => {
    renderBottomNav("/news/events/evt-1");

    // The same `isActive` predicate the sidebar uses, so the two presentations cannot disagree about where
    // the reader is. `NavLink` would decide by prefix and light up both.
    expect(screen.getByRole("link", { name: "事件流" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "持仓异动" })).not.toHaveAttribute("aria-current");
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("marks the OI monitor current on the OI route", () => {
    renderBottomNav("/news/oi");

    expect(screen.getByRole("link", { name: "持仓异动" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "事件流" })).not.toHaveAttribute("aria-current");
  });

  it("has no destination for the pipeline status page", () => {
    // #207: the page is still there and still reachable from the topbar lamp; what it no longer has is a
    // slot on a bar with 48px targets, where every slot has to earn its width.
    renderBottomNav("/news/status");

    expect(screen.queryByRole("link", { name: "流水线状态" })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("link", { current: "page" })).toHaveLength(0);
  });
});

function renderBottomNav(route = "/news") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppBottomNav />
    </MemoryRouter>,
  );
}
