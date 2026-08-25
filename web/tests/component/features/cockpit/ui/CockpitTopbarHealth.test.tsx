import { CockpitTopbar, type CockpitHealth } from "@features/cockpit/ui/CockpitTopbar";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => cleanup());

/**
 * The topbar health lamp (#207). It replaced two things at once: the sidebar's health dot, which only ever
 * reached the widest frame, and the feed header's pill, which only ever reached one route.
 *
 * The rule the whole design rests on is that a healthy pipeline renders nothing at all. A light that is
 * always on is one the reader learns to stop seeing, which is exactly why 流水线状态 stopped being worth a
 * navigation slot.
 */
describe("CockpitTopbar health lamp", () => {
  it("renders nothing at all while the pipeline is healthy", () => {
    renderTopbar(null);

    expect(screen.queryByRole("button", { name: /流水线健康/ })).not.toBeInTheDocument();
    expect(document.querySelector(".topbar-health-lamp")).toBeNull();
  });

  it("shows the failing item's own sentence at warn, in the caution tone", () => {
    renderTopbar(healthFixture({ level: "warn" }));

    const lamp = screen.getByRole("button", { name: "流水线健康：24 小时降级率 8%（14/175）" });
    expect(lamp).toHaveAttribute("data-level", "warn");
    expect(lamp).toHaveTextContent("24 小时降级率 8%（14/175）");
  });

  it("carries the alert level through to the lamp when the pipeline is bad", () => {
    renderTopbar(healthFixture({ headline: "流水线异常", level: "bad" }));

    expect(screen.getByRole("button", { name: /流水线健康/ })).toHaveAttribute("data-level", "bad");
  });

  it("opens the four stage lines and a door to the status page, and closes on Esc", async () => {
    renderTopbar(healthFixture({ level: "warn" }));

    const lamp = screen.getByRole("button", { name: /流水线健康/ });
    expect(lamp).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(lamp);

    const popover = await screen.findByRole("dialog");
    expect(lamp).toHaveAttribute("aria-expanded", "true");
    // Every stage the server reports, each with its own level and its own sentence — the frame maps nothing.
    const items = within(popover).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "Ingest已连接，正在收帧",
      "Broker队列畅通",
      "Model24 小时降级率 8%（14/175）",
      "Delivery24 小时已推送 41 条",
    ]);
    expect(within(popover).getByRole("link", { name: /打开流水线状态/ })).toHaveAttribute(
      "href",
      "/news/status",
    );

    // Radix owns the dismiss layer and the key handler; the console adds no `keydown` listener of its own.
    fireEvent.keyDown(popover, { key: "Escape" });
    expect(await screen.findByRole("button", { name: /流水线健康/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

function healthFixture(overrides: Partial<CockpitHealth> = {}): CockpitHealth {
  return {
    headline: "流水线注意",
    items: [
      { key: "ingest", label: "Ingest", level: "ok", summary: "已连接，正在收帧" },
      { key: "broker", label: "Broker", level: "ok", summary: "队列畅通" },
      { key: "model", label: "Model", level: "warn", summary: "24 小时降级率 8%（14/175）" },
      { key: "delivery", label: "Delivery", level: "ok", summary: "24 小时已推送 41 条" },
    ],
    level: "warn",
    summary: "24 小时降级率 8%（14/175）",
    to: "/news/status",
    ...overrides,
  };
}

function renderTopbar(health: CockpitHealth | null) {
  return render(
    <MemoryRouter>
      <CockpitTopbar
        health={health}
        onRefresh={vi.fn()}
        search={{ onSubmitQuery: vi.fn(), query: "" }}
        status={{ configReady: true, status: null, statusError: false, statusLoading: false }}
        title="新闻事件流"
      />
    </MemoryRouter>,
  );
}
