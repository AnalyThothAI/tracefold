import { MacroModuleSections } from "@features/macro/ui/MacroModuleSections";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { macroModuleFixture } from "@tests/fixtures/macroFixture";
import { afterEach, describe, expect, it } from "vitest";

describe("macro module evidence workbench", () => {
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", window.location.pathname);
    window.location.hash = "";
  });

  it("renders only the hash-selected natural-frequency category", () => {
    window.location.hash = "#curve";
    const module = macroModuleFixture("rates_fed");

    render(<MacroModuleSections module={module} />);

    expect(screen.getByRole("heading", { name: "牛陡 · 当前 / 1W / 1M / 3M" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "名义 Treasury 曲线" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "政策走廊与当前市场定价" })).toBeNull();
    expect(screen.getByText("实际利率是本次主线的主要驱动。")).toBeVisible();
  });

  it("switches category through the compact selector without mounting peer panels", async () => {
    const module = macroModuleFixture("rates_fed");
    render(<MacroModuleSections module={module} />);

    fireEvent.change(screen.getByLabelText("利率与美联储当前视图"), {
      target: { value: "policy" },
    });

    expect(window.location.hash).toBe("#policy");
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "政策走廊与当前市场定价" })).toBeVisible(),
    );
    expect(screen.queryByRole("heading", { name: "牛陡 · 当前 / 1W / 1M / 3M" })).toBeNull();
  });

  it("uses the stable closed-condition identity for chart annotations", () => {
    window.location.hash = "#policy";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("rates fixture mismatch");
    module.thesis_context.conditions = [
      {
        ...module.thesis_context.conditions[0]!,
        condition_id: "rates.real10y.tail:fred.dff:leq20",
        candidate_id: "rates.real10y.tail:fred.dff:leq20",
        dataset_id: "fred.dff",
      },
    ];

    const { container } = render(<MacroModuleSections module={module} />);

    expect(
      container.querySelector(
        '[data-annotation-id="thesis:mainline:mainline:confirmation:rates.real10y.tail:fred.dff:leq20"]',
      ),
    ).not.toBeNull();
    expect(screen.getByText(/闭集条件 1/)).toBeVisible();
  });

  it("renders CFE settlement expiry only in the volatility term structure", () => {
    window.location.hash = "#term";
    const volatility = macroModuleFixture("volatility");
    const rendered = render(<MacroModuleSections module={volatility} />);

    expect(screen.getByRole("heading", { name: "VIX 期限结构" })).toBeVisible();
    expect(screen.getByText("VXQ26")).toBeVisible();
    expect(screen.getByText("2026-08-19")).toBeVisible();
    rendered.unmount();

    window.location.hash = "#futures";
    render(<MacroModuleSections module={macroModuleFixture("cross_asset")} />);
    expect(screen.getByRole("heading", { name: "期货市场与仓位确认" })).toBeVisible();
    expect(screen.queryByText("VXQ26")).toBeNull();
  });

  it("omits empty contradiction, falsifier, and checkpoint rails", () => {
    render(<MacroModuleSections module={macroModuleFixture("credit")} />);

    expect(screen.queryByText("矛盾")).toBeNull();
    expect(screen.queryByText("失效条件")).toBeNull();
    expect(screen.queryByText("下一检查点")).toBeNull();
  });
});
