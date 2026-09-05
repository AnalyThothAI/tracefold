import { CockpitWorkersCapabilities } from "@features/cockpit";
import { cleanup, render, screen, within } from "@testing-library/react";
import { axe } from "jest-axe";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => {
  cleanup();
});

/**
 * #553 PR-3. A faulted Trading lane, an unbuildable push sender and an unassemblable News Program no
 * longer stop the Workers process, so `workers_runtime.state` reads `running` beside a dead
 * capability. This list is the only place a console reader learns which one stopped, and why.
 */
const capabilities = {
  news_delivery: {
    reason: "news_item_push_telegram_bot_token_unavailable",
    state: "unavailable" as const,
  },
  news_ingestion: { reason: null, state: "running" as const },
  news_quotes: { reason: "news_quotes_not_configured", state: "disabled" as const },
  trading_signal_lane: { reason: "trading_signal_lane:RuntimeError", state: "faulted" as const },
};

function rowFor(label: string): HTMLElement {
  const term = screen.getByText(label);
  const row = term.closest(".cockpit-capability");
  if (!(row instanceof HTMLElement)) throw new Error(`no capability row for ${label}`);
  return row;
}

describe("CockpitWorkersCapabilities", () => {
  it("names every capability's state and the reason the server gave", () => {
    render(<CockpitWorkersCapabilities capabilities={capabilities} />);

    const lane = rowFor("交易信号 lane");
    expect(lane).toHaveAttribute("data-state", "faulted");
    expect(within(lane).getByText("已故障")).toBeInTheDocument();
    expect(within(lane).getByText("trading_signal_lane:RuntimeError")).toBeInTheDocument();

    const delivery = rowFor("推送发送");
    expect(delivery).toHaveAttribute("data-state", "unavailable");
    expect(within(delivery).getByText("不可用")).toBeInTheDocument();
    expect(
      within(delivery).getByText("news_item_push_telegram_bot_token_unavailable"),
    ).toBeInTheDocument();

    // A healthy capability still reports, so "everything is fine" is a statement and not a silence.
    const ingestion = rowFor("接收与入库");
    expect(ingestion).toHaveAttribute("data-state", "running");
    expect(within(ingestion).getByText("运行中")).toBeInTheDocument();
  });

  it("leads with what stopped, not with the alphabet", () => {
    const { container } = render(<CockpitWorkersCapabilities capabilities={capabilities} />);

    const states = Array.from(container.querySelectorAll(".cockpit-capability")).map((row) =>
      row.getAttribute("data-state"),
    );

    expect(states).toEqual(["faulted", "unavailable", "disabled", "running"]);
  });

  it("says the report is missing rather than implying every capability is healthy", () => {
    const { container } = render(<CockpitWorkersCapabilities capabilities={{}} />);

    expect(screen.getByText("Workers 尚未上报能力状态")).toBeInTheDocument();
    expect(container.querySelector(".cockpit-capability")).toBeNull();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<CockpitWorkersCapabilities capabilities={capabilities} />);

    expect(await axe(container)).toHaveNoViolations();
  });
});
