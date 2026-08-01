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

    expect(screen.getByRole("heading", { name: "扭转式陡峭化 · 1D 主时钟" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "名义 Treasury 曲线" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "政策走廊与当前市场定价" })).toBeNull();
    expect(screen.getAllByText("前一交易日").length).toBeGreaterThan(0);
    expect(screen.queryByText("1周基准")).toBeNull();

    fireEvent.click(screen.getByRole("checkbox", { name: "1W" }));

    expect(screen.getByText("1周基准 · 2026-07-22")).toBeVisible();
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
    expect(screen.queryByRole("heading", { name: "扭转式陡峭化 · 1D 主时钟" })).toBeNull();
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
