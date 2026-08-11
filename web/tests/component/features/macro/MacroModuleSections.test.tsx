import { MacroModuleSections } from "@features/macro/ui/MacroModuleSections";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

  it("does not render a zero-value Fed distribution as a valid chart when state is no_call", () => {
    window.location.hash = "#fed";

    render(<MacroModuleSections module={macroModuleFixture("rates_fed")} />);

    const distribution = screen.getByRole("region", { name: "官员沟通分布" });
    expect(within(distribution).getByText("状态").nextElementSibling).toHaveTextContent(
      "未形成判断",
    );
    expect(within(distribution).getByText("截止日").nextElementSibling).toHaveTextContent(
      "尚无截止日",
    );
    expect(within(distribution).getByText("观察窗口").nextElementSibling).toHaveTextContent(
      "45 日",
    );
    expect(within(distribution).getByText("已分析事件").nextElementSibling).toHaveTextContent("0");
    expect(within(distribution).getByText("独立官员").nextElementSibling).toHaveTextContent("0");
    expect(within(distribution).getByText("不确定事件").nextElementSibling).toHaveTextContent("0");
    expect(screen.queryByRole("img", { name: /已审阅沟通分布/ })).toBeNull();

    const roster = screen.getByRole("region", { name: "美联储官员名册" });
    expect(within(roster).getByText("不可用")).toBeVisible();
    expect(within(roster).getByText("effective_dated_roster_not_ingested")).toBeVisible();
  });

  it("shows disabled document analysis as optional without degrading official Fed facts", () => {
    window.location.hash = "#fed";

    render(<MacroModuleSections module={macroModuleFixture("rates_fed")} />);

    const runtime = screen.getByRole("region", { name: "文档分析运行态" });
    expect(within(runtime).getByText("运行状态").nextElementSibling).toHaveTextContent("未启用");
    expect(
      within(runtime).getByText("可选文档分析未启用；不影响利率与美联储官方事实健康。"),
    ).toBeVisible();
    expect(within(runtime).getByText("已启用").nextElementSibling).toHaveTextContent("否");
    expect(within(runtime).getByText("网关已配置").nextElementSibling).toHaveTextContent("否");
    expect(within(runtime).getByText("Worker 装配条件").nextElementSibling).toHaveTextContent(
      "不满足",
    );
    expect(within(runtime).getByText("模型（运行审计）").nextElementSibling).toHaveTextContent(
      "gpt-5.4-mini",
    );
    expect(within(runtime).queryByText(/模型判断|model call/i)).toBeNull();
  });

  it.each([
    {
      configured: false,
      description: "可选文档分析已启用，但模型网关未配置；官方事实健康不由此状态降级。",
      label: "未配置",
      state: "unconfigured" as const,
      workerActive: false,
    },
    {
      configured: true,
      description:
        "文档分析已启用且模型网关已配置；这表示 Worker 可被运行时装配，不代表进程存活。分析结果属于独立证据轨，不替代官方事实。",
      label: "可装配",
      state: "active" as const,
      workerActive: true,
    },
  ])("renders the $state document analysis runtime literally", (scenario) => {
    window.location.hash = "#fed";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("expected rates fixture");
    module.document_analysis_runtime = {
      configured: scenario.configured,
      enabled: true,
      model: "same-runtime-model",
      state: scenario.state,
      worker_active: scenario.workerActive,
    };

    render(<MacroModuleSections module={module} />);

    const runtime = screen.getByRole("region", { name: "文档分析运行态" });
    expect(within(runtime).getByText("运行状态").nextElementSibling).toHaveTextContent(
      scenario.label,
    );
    expect(within(runtime).getByText(scenario.description)).toBeVisible();
    expect(within(runtime).getByText("已启用").nextElementSibling).toHaveTextContent("是");
    expect(within(runtime).getByText("网关已配置").nextElementSibling).toHaveTextContent(
      scenario.configured ? "是" : "否",
    );
    expect(within(runtime).getByText("Worker 装配条件").nextElementSibling).toHaveTextContent(
      scenario.workerActive ? "满足" : "不满足",
    );
    expect(within(runtime).getByText("模型（运行审计）").nextElementSibling).toHaveTextContent(
      "same-runtime-model",
    );
  });

  it("renders the reviewed Fed distribution and current effective-dated roster", () => {
    window.location.hash = "#fed";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("expected rates fixture");
    module.fed.officials_distribution = {
      analyzed_event_count: 6,
      as_of: "2026-07-28",
      not_policy_signal_event_count: 1,
      stance_event_counts: { dovish: 1, hawkish: 2, mixed: 1, neutral: 1 },
      stance_unique_official_counts: { dovish: 1, hawkish: 2, mixed: 1, neutral: 1 },
      state: "current",
      uncertain_event_count: 1,
      unique_official_count: 5,
      window_days: 45,
    };
    module.fed.roster = {
      officials: [
        {
          effective_end: null,
          effective_start: "2026-01-01",
          fomc_participant: true,
          fomc_voter: true,
          official_id: "chair",
          official_name: "Jerome Powell",
          organization: "Federal Reserve Board",
          role_fact_id: "fed-role:chair:2026",
          role_title: "Chair",
          source_url: "https://www.federalreserve.gov/",
        },
      ],
      reason: null,
      state: "current",
    };

    render(<MacroModuleSections module={module} />);

    expect(screen.getByRole("img", { name: "近 45 日已审阅沟通分布" })).toBeVisible();
    const roster = screen.getByRole("region", { name: "美联储官员名册" });
    expect(within(roster).getByText("当前")).toBeVisible();
    expect(within(roster).getByText("Jerome Powell")).toBeVisible();
    expect(within(roster).getByText(/Chair · Federal Reserve Board/)).toBeVisible();
  });

  it("renders the official FOMC meeting calendar with revision and source clocks", () => {
    window.location.hash = "#fed";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("expected rates fixture");
    module.fed.meeting_calendar.meetings.unshift({
      meeting_id: "FOMC:2025-12-09:2025-12-10",
      start_date: "2025-12-09",
      end_date: "2025-12-10",
      has_sep: true,
      calendar_published_at_ms: null,
      received_at_ms: module.fed.meeting_calendar.meetings[0]?.received_at_ms ?? 0,
      source_url: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    });

    render(<MacroModuleSections module={module} />);

    const calendar = screen.getByRole("region", { name: "官方 FOMC 会议日历" });
    expect(within(calendar).getByText("fomc-calendar-2026-07-28")).toBeVisible();
    expect(within(calendar).getByText("2026-09-15—2026-09-16")).toBeVisible();
    expect(within(calendar).queryByText("2025-12-09—2025-12-10")).toBeNull();
    expect(within(calendar).getByText("包含 SEP")).toBeVisible();
    expect(within(calendar).getByText(/日历发布/)).toBeVisible();
    expect(within(calendar).getByText(/系统接收/)).toBeVisible();
    expect(
      within(calendar).getByRole("link", { name: "Federal Reserve 原始日历" }),
    ).toHaveAttribute("href", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm");
  });

  it("renders official Treasury auction results without deriving an auction score", () => {
    window.location.hash = "#policy";

    render(<MacroModuleSections module={macroModuleFixture("rates_fed")} />);

    const auctions = screen.getByRole("region", { name: "美国国债拍卖结果" });
    expect(within(auctions).getByText("10-Year Note")).toBeVisible();
    expect(within(auctions).getByText("91282CQB0 · 2026-07-27")).toBeVisible();
    expect(within(auctions).getByText("2.67")).toBeVisible();
    expect(within(auctions).getByText("4.321%")).toBeVisible();
    expect(within(auctions).getByText("42,000,000,000 USD")).toBeVisible();
    expect(within(auctions).getByText("间接 70% · 直接 12.5% · 一级交易商 17.5%")).toBeVisible();
    expect(within(auctions).queryByText(/score|评分/i)).toBeNull();
    expect(within(auctions).getByRole("link", { name: "Treasury 原始拍卖结果" })).toHaveAttribute(
      "href",
      "https://fiscal.treasury.gov/reports-statements/treasury-auctions/",
    );
  });

  it("shows explicit gaps for missing Treasury auction result fields", () => {
    window.location.hash = "#policy";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("expected rates fixture");
    const auction = module.treasury_auctions.recent_results[0];
    if (!auction) throw new Error("expected auction fixture");
    Object.assign(auction, {
      bid_to_cover_ratio: null,
      direct_award_share_pct: null,
      high_yield_pct: null,
      indirect_award_share_pct: null,
      offering_amount_usd: null,
      primary_dealer_award_share_pct: null,
    });

    render(<MacroModuleSections module={module} />);

    const auctions = screen.getByRole("region", { name: "美国国债拍卖结果" });
    expect(within(auctions).getAllByText("—")).toHaveLength(3);
    expect(within(auctions).getByText("间接 — · 直接 — · 一级交易商 —")).toBeVisible();
  });

  it("omits empty contradiction, falsifier, and checkpoint rails", () => {
    render(<MacroModuleSections module={macroModuleFixture("credit")} />);

    expect(screen.queryByText("矛盾")).toBeNull();
    expect(screen.queryByText("失效条件")).toBeNull();
    expect(screen.queryByText("下一检查点")).toBeNull();
  });
});
