import { CockpitTopbar, type CockpitHealth } from "@features/cockpit/ui/CockpitTopbar";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => cleanup());

/**
 * The topbar health lamp (#207). It replaced two things at once: the sidebar's health dot, which only ever
 * reached the widest frame, and the feed header's pill, which only ever reached one route.
 *
 * A null value means the shell has not read health yet. It renders no diagnosis until the route supplies
 * one; an explicit healthy result is a CockpitHealth value with level `ok`.
 */
describe("CockpitTopbar health lamp", () => {
  it("renders no diagnosis before pipeline health has been read", () => {
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

  it("keeps approved visible copy while the accessible diagnosis remains specific", () => {
    renderTopbar(healthFixture({ buttonText: "流水线", level: "bad" }));

    const lamp = screen.getByRole("button", { name: "流水线健康：24 小时降级率 8%（14/175）" });
    expect(lamp).toHaveTextContent("流水线");
    expect(lamp).not.toHaveTextContent("24 小时降级率 8%（14/175）");
  });

  it("opens the compact pipeline facts and a door to the status page, and closes on Esc", async () => {
    renderTopbar(healthFixture({ level: "warn" }));

    const lamp = screen.getByRole("button", { name: /流水线健康/ });
    expect(lamp).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(lamp);

    const popover = await screen.findByRole("dialog");
    expect(lamp).toHaveAttribute("aria-expanded", "true");
    expect(within(popover).queryByText("流水线注意")).not.toBeInTheDocument();
    const items = within(popover).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "接入正常 · 已连接，正在收帧",
      "队列正常 · 队列畅通",
      "模型注意 · 24 小时降级率 8%（14/175）",
      "推送正常 · 24 小时已推送 41 条",
      "标的表2,344 份交易合约",
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
      { key: "ingest", label: "接入", level: "ok", summary: "正常 · 已连接，正在收帧" },
      { key: "broker", label: "队列", level: "ok", summary: "正常 · 队列畅通" },
      { key: "model", label: "模型", level: "warn", summary: "注意 · 24 小时降级率 8%（14/175）" },
      { key: "delivery", label: "推送", level: "ok", summary: "正常 · 24 小时已推送 41 条" },
      { key: "instruments", label: "标的表", level: null, summary: "2,344 份交易合约" },
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
        search={{ onSubmitQuery: vi.fn(), query: "" }}
        status={{ configReady: true, status: null, statusError: false, statusLoading: false }}
        title="新闻事件流"
      />
    </MemoryRouter>,
  );
}
