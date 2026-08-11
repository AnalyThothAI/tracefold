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

  it("canonicalizes an invalid hash to the module default section", async () => {
    window.location.hash = "#retired-view";

    render(<MacroModuleSections module={macroModuleFixture("volatility")} />);

    await waitFor(() => expect(window.location.hash).toBe("#term"));
    expect(screen.getByRole("tab", { name: "现货–3M" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "VIX 期限结构" })).toBeVisible();
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

  it("labels normalized volatility as a base-100 comparison instead of raw index points", () => {
    window.location.hash = "#cross-asset";
    const module = macroModuleFixture("volatility");
    if (module.module_id !== "volatility") throw new Error("expected volatility fixture");
    module.cross_asset_implied.normalized_groups = [
      {
        display_order: 1,
        group_id: "implied-volatility",
        label: "隐含波动率归一化",
        series: [
          {
            display_order: 1,
            label: "VXN",
            points: [
              { date: "2026-07-21", normalized_value: 100 },
              { date: "2026-07-29", normalized_value: 104.2 },
            ],
            source: {
              dataset_id: "cboe.vxn",
              fact: null,
              label: "CBOE VXN",
              source_role: "decision_primary",
            },
            symbol: "VXN",
          },
        ],
      },
    ];

    render(<MacroModuleSections module={module} />);

    expect(screen.getAllByText(/基期=100/).length).toBeGreaterThan(0);
    expect(screen.getByText(/最新：VXN 104\.2（基期=100）/)).toBeVisible();
    expect(screen.queryByText(/VXN 104\.2 点/)).toBeNull();
  });

  it("shows separate latest and return sources, as-of clocks, and the price unit", () => {
    window.location.hash = "#returns";
    const module = macroModuleFixture("cross_asset");
    if (module.module_id !== "cross_asset") throw new Error("expected cross-asset fixture");
    module.assets.return_matrix = [
      {
        display_order: 1,
        group_id: "equities",
        group_label: "股票",
        identity_policy: "separate_source_facts_no_blend",
        label: "标普 500 ETF",
        latest_source: {
          dataset_id: "market.spy.intraday",
          fact: {
            as_of: "2026-07-29",
            change_1d_pct: 0.4,
            change_1m_pct: 2.1,
            change_1w_pct: 1.2,
            dataset_id: "market.spy.intraday",
            latest_value: 501.25,
            market_time_ms: Date.parse("2026-07-29T19:59:00Z"),
            source_url: "https://example.com/spy/latest",
            unit: "price",
          },
          label: "SPY 实时源",
          source_role: "intraday_proxy",
        },
        return_source: {
          dataset_id: "market.spy.daily",
          fact: {
            as_of: "2026-07-28",
            change_1d_pct: 0.3,
            change_1m_pct: 2,
            change_1w_pct: 1.1,
            dataset_id: "market.spy.daily",
            latest_value: 499.25,
            market_time_ms: Date.parse("2026-07-28T20:00:00Z"),
            source_url: "https://example.com/spy/returns",
            unit: "price",
          },
          label: "SPY 日收益源",
          source_role: "decision_primary",
        },
        selection_policy: "intraday_latest_and_daily_returns_exact",
        symbol: "SPY",
      },
    ];

    render(<MacroModuleSections module={module} />);

    const row = screen.getByText("SPY").closest('[role="row"]');
    if (!(row instanceof HTMLElement)) throw new Error("expected SPY row");
    expect(within(row).getByText("501.25（价格）")).toBeVisible();
    expect(within(row).getByText("最新：SPY 实时源 · 2026-07-29")).toBeVisible();
    expect(within(row).getByText("收益：SPY 日收益源 · 2026-07-28")).toBeVisible();
    expect(within(row).queryByText(/单位未解释/)).toBeNull();
  });

  it("puts server-owned benchmark facts and source clocks on the cross-asset first view", () => {
    window.location.hash = "#returns";
    const module = macroModuleFixture("cross_asset");
    if (module.module_id !== "cross_asset") throw new Error("expected cross-asset fixture");
    module.assets.source_identity = [
      ["WTI", "原油", "fred.wti", 74.5, "usd_per_barrel"],
      ["BTC", "比特币", "market.btc", 66_500, "usdt"],
      ["VIX", "VIX", "fred.vix", 15.2, "index"],
      ["SPY", "标普代理", "market.spy", 501.25, "price"],
      ["UUP", "美元代理", "market.uup", 28.4, "price"],
    ].map(([symbol, label, datasetId, latestValue, unit], index) => ({
      display_order: index + 1,
      evidence_kind: index < 3 ? "benchmark" : "fixed_etf_proxy",
      identity_policy: "separate_source_facts_no_blend" as const,
      label: String(label),
      selection_policy: "server_owned_exact_source",
      sources: [
        {
          dataset_id: String(datasetId),
          fact: {
            as_of: "2026-07-29",
            change_1d_pct: null,
            change_1m_pct: null,
            change_1w_pct: null,
            dataset_id: String(datasetId),
            latest_value: Number(latestValue),
            market_time_ms: Date.parse("2026-07-29T20:00:00Z"),
            source_url: `https://example.com/${String(symbol).toLowerCase()}`,
            unit: String(unit),
          },
          label: `${String(symbol)} 服务端事实源`,
          source_role: "decision_primary",
        },
      ],
      symbol: String(symbol),
    }));

    render(<MacroModuleSections module={module} />);

    const strip = screen.getByRole("region", { name: "基准与来源事实" });
    expect(within(strip).getByText("74.5 美元/桶")).toBeVisible();
    expect(within(strip).getByText("66,500 USDT")).toBeVisible();
    expect(within(strip).getByText("15.2 点")).toBeVisible();
    expect(within(strip).getAllByText("2026-07-29")).toHaveLength(5);
    expect(within(strip).queryByText(/分数|score/i)).toBeNull();
  });

  it("selects correlation windows from the server contract and explains matrix derivation", () => {
    window.location.hash = "#correlations";
    const module = macroModuleFixture("cross_asset");
    if (module.module_id !== "cross_asset") throw new Error("expected cross-asset fixture");
    module.correlations = [
      {
        correlation: 0.1,
        left: "SPY",
        right: "TLT",
        sample_count: 30,
        window: "30_daily_returns",
      },
      {
        correlation: 0.5,
        left: "SPY",
        right: "TLT",
        sample_count: 90,
        window: "90_daily_returns",
      },
      {
        correlation: 0.8,
        left: "SPY",
        right: "TLT",
        sample_count: 252,
        window: "252_daily_returns",
      },
    ];

    const rendered = render(<MacroModuleSections module={module} />);

    const selector = screen.getByRole("combobox", { name: "相关收益窗口" });
    expect(selector).toHaveValue("90_daily_returns");
    expect(screen.getByText(/最少共同观测：20/)).toBeVisible();
    expect(screen.getByText(/undirected_pairs_mirrored_with_unit_diagonal/)).toBeVisible();
    expect(screen.getByLabelText("SPY 与 TLT 相关性 0.50")).toBeVisible();
    expect(screen.getByLabelText("TLT 与 SPY 相关性 0.50")).toBeVisible();
    expect(screen.getByLabelText("SPY 与 SPY 相关性 1.00")).toBeVisible();

    fireEvent.change(selector, { target: { value: "252_daily_returns" } });

    expect(screen.getByLabelText("SPY 与 TLT 相关性 0.80")).toBeVisible();
    expect(screen.queryByLabelText("SPY 与 TLT 相关性 0.50")).toBeNull();

    module.correlation_contract = {
      ...module.correlation_contract,
      default_window: "30_daily_returns",
      supported_windows: ["30_daily_returns"],
    };
    rendered.rerender(<MacroModuleSections module={{ ...module }} />);

    expect(selector).toHaveValue("30_daily_returns");
    expect(screen.getByLabelText("SPY 与 TLT 相关性 0.10")).toBeVisible();
  });

  it("shows the economy release reference period and all publication clocks", () => {
    window.location.hash = "#inflation";
    const module = macroModuleFixture("economy_inflation");
    if (module.module_id !== "economy_inflation") throw new Error("expected economy fixture");
    module.inflation.official_releases = [
      {
        actual_value: 2.7,
        dataset_id: "bls.cpi",
        estimate_value: 2.6,
        label: "CPI 同比",
        observations: [
          {
            actual_value: 2.7,
            estimate_value: 2.6,
            prior_value: 2.5,
            published_at_ms: Date.parse("2026-07-14T12:30:00Z"),
            received_at_ms: Date.parse("2026-07-14T12:31:00Z"),
            reference_period: "2026-06",
            revised_prior_value: 2.5,
            revision: 0,
            scheduled_at_ms: Date.parse("2026-07-14T12:30:00Z"),
            seasonal_adjustment: "seasonally_adjusted",
            source_url: "https://www.bls.gov/cpi/",
            surprise: 0.1,
            unit: "percent",
          },
        ],
        prior_value: 2.5,
        published_at_ms: Date.parse("2026-07-14T12:30:00Z"),
        received_at_ms: Date.parse("2026-07-14T12:31:00Z"),
        reference_period: "2026-06",
        revised_prior_value: 2.5,
        revision: 0,
        scheduled_at_ms: Date.parse("2026-07-14T12:30:00Z"),
        seasonal_adjustment: "seasonally_adjusted",
        source_url: "https://www.bls.gov/cpi/",
        surprise: 0.1,
        unit: "percent",
      },
    ];

    render(<MacroModuleSections module={module} />);

    const release = screen.getByRole("region", { name: "官方发布事实" });
    expect(within(release).getByText("CPI 同比 · 参考期 2026-06")).toBeVisible();
    expect(within(release).getByText("调整：季节调整")).toBeVisible();
    expect(within(release).getByText(/计划 .* · 发布 .* · 接收/)).toBeVisible();
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

  it("renders upcoming FOMC meetings and keeps the complete official calendar reachable", () => {
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
    const upcomingMeeting = within(calendar).getByText("2026-09-15—2026-09-16").closest("article");
    if (!(upcomingMeeting instanceof HTMLElement)) throw new Error("expected upcoming meeting");
    const completeCalendar = within(calendar)
      .getByText("查看完整会议日历（2 次）")
      .closest("details");
    if (!(completeCalendar instanceof HTMLDetailsElement)) {
      throw new Error("expected complete meeting calendar audit");
    }
    expect(within(completeCalendar).getByText("2025-12-09—2025-12-10")).toBeInTheDocument();
    expect(within(upcomingMeeting).getByText("包含 SEP")).toBeVisible();
    expect(within(upcomingMeeting).getByText(/日历发布/)).toBeVisible();
    expect(within(upcomingMeeting).getByText(/系统接收/)).toBeVisible();
    expect(
      within(upcomingMeeting).getByRole("link", { name: "Federal Reserve 原始日历" }),
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

  it("uses the contract's exact discount and investment rate labels for Treasury Bills", () => {
    window.location.hash = "#policy";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("expected rates fixture");
    const auction = module.treasury_auctions.recent_results[0];
    if (!auction) throw new Error("expected auction fixture");
    Object.assign(auction, {
      high_discount_rate_pct: 5.2,
      high_investment_rate_pct: 5.35,
      high_yield_pct: null,
      security_term: "4-Week Bill",
    });

    render(<MacroModuleSections module={module} />);

    const auctions = screen.getByRole("region", { name: "美国国债拍卖结果" });
    expect(within(auctions).getByText("最高贴现率").nextElementSibling).toHaveTextContent("5.2%");
    expect(within(auctions).getByText("最高投资率").nextElementSibling).toHaveTextContent("5.35%");
    expect(within(auctions).queryByText("最高得标收益率")).toBeNull();
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

  it("keeps credit funding formula and input-fact lineage reachable", () => {
    window.location.hash = "#funding";
    const module = macroModuleFixture("credit");
    if (module.module_id !== "credit") throw new Error("expected credit fixture");
    module.funding_costs.comparisons = [
      {
        as_of: "2026-07-28",
        corporate_dataset_id: "fred.bamlh0a0hym2ey",
        formula_version: "matched_rate_difference_v1",
        input_fact_ids: ["fact:hy:2026-07-28", "fact:ust10y:2026-07-28"],
        label: "HY 减 10Y Treasury",
        reference_dataset_id: "treasury.daily_nominal_curve",
        value_bp: 310,
      },
    ];

    render(<MacroModuleSections module={module} />);

    const audit = screen.getByText("融资比较审计（1）").closest("details");
    if (!(audit instanceof HTMLDetailsElement)) throw new Error("expected funding audit");
    expect(audit).toHaveTextContent("matched_rate_difference_v1");
    expect(audit).toHaveTextContent("fred.bamlh0a0hym2ey");
    expect(audit).toHaveTextContent("treasury.daily_nominal_curve");
    expect(audit).toHaveTextContent("fact:hy:2026-07-28");
    expect(audit).toHaveTextContent("2026-07-28");
  });
});
