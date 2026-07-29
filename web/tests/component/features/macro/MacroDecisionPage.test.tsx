import { MacroModulePage, MacroOverviewPage } from "@features/macro";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { macroModuleFixture, macroOverviewFixture } from "@tests/fixtures/macroFixture";
import { server } from "@tests/msw/server";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { axe } from "jest-axe";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

describe("Macro Thesis workbench", () => {
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", window.location.pathname);
  });

  it("renders a 30-second brief with server-grouped assets and scoped live evidence", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );
    const view = renderWithProviders(<MacroOverviewPage token="test-token" />, {
      route: "/macro",
    });

    expect(
      await screen.findByRole("heading", { name: "实际利率上行主导短期风险资产定价" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "当前核心矛盾" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "十二资产：事实动量 vs 条件展望" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "主线、矛盾与资产的新增事实" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Actionable" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Watch" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Evidence gap" })).toBeVisible();
    const actionable = renderedAssetSymbols("Actionable");
    const watch = renderedAssetSymbols("Watch");
    const evidenceGap = renderedAssetSymbols("Evidence gap");
    expect(actionable).toHaveLength(4);
    expect(watch).toHaveLength(4);
    expect(evidenceGap).toHaveLength(4);
    expect(new Set([...actionable, ...watch, ...evidenceGap]).size).toBe(12);
    expectAssetGroupCount("Actionable", 4);
    expectAssetGroupCount("Watch", 4);
    expectAssetGroupCount("Evidence gap", 4);
    expect(screen.getAllByText("正在确认").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/美国高收益信用利差/).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "主线证据入口" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Evidence Health" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "数据质量与真实缺口" })).toBeVisible();
    expect(screen.queryByLabelText("影响当前判断的异常状态")).toBeNull();
    expect(screen.queryByText("no_call")).toBeNull();
    expect(screen.queryByText(/analysis:/)).toBeNull();
    expect(screen.getAllByRole("link", { name: /利率与美联储/ }).length).toBeGreaterThan(0);
    expect(screen.getByText(/观察：1 周变化 ≤ -25bp/)).toBeVisible();
    expect(
      document.querySelectorAll('.macro-decision__status-glyph[aria-hidden="true"]').length,
    ).toBeGreaterThan(0);
    expect(
      document.querySelector('.macro-decision__status-glyph[data-tone="positive"]'),
    ).not.toBeNull();
    expect(
      document.querySelector('.macro-decision__status-glyph[data-tone="negative"]'),
    ).not.toBeNull();
    expect(
      document.querySelector('.macro-decision__status-glyph[data-tone="caution"]'),
    ).not.toBeNull();
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it("renders only the server reader projection when the immutable Thesis contains machine text", async () => {
    const overview = macroOverviewFixture();
    if (!overview.thesis || !overview.mainline_presentation) {
      throw new Error("published overview fixture is missing");
    }
    overview.thesis.mainline.title = "rates_fed no_call";
    overview.thesis.mainline.thesis = "macro-module:2026-07-27:rates_fed";
    overview.thesis.mainline.claims[0]!.statement = "equity_valuation";
    overview.thesis.module_assessments[0]!.analysis = "数据 degraded · macro-module:rates_fed";
    overview.thesis.assets[0]!.outlook_1w.causal_channel = "cross_asset no_call";
    overview.mainline_presentation.title = {
      text: "读者主线标题",
      origin: "structured_fallback",
    };
    overview.mainline_presentation.thesis = {
      text: "从论点、证据、反证与条件链阅读本期判断。",
      origin: "structured_fallback",
    };
    overview.claim_presentation[0]!.statement = {
      text: "利率条件通过贴现率影响股票估值。",
      origin: "structured_fallback",
    };
    overview.claim_presentation[0]!.module_evidence[0]!.reader_narrative = {
      text: "利率与美联储作为本论点的驱动证据。",
      origin: "structured_fallback",
    };
    overview.asset_presentation[0]!.horizons[0]!.reader_rationale = {
      text: "SPY 的一周事实动量与方向条件已分开登记。",
      origin: "structured_fallback",
    };
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );

    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    const judgment = (await screen.findByRole("heading", { name: "读者主线标题" })).closest(
      "section",
    );
    const evidence = screen.getByRole("heading", { name: "主线证据入口" }).closest("section");
    if (!judgment || !evidence) throw new Error("reader projection sections did not render");
    expect(judgment).toHaveTextContent("利率条件通过贴现率影响股票估值。");
    expect(judgment).not.toHaveTextContent(/rates_fed|no_call|macro-module:|equity_valuation/);
    expect(evidence).toHaveTextContent("利率与美联储作为本论点的驱动证据。");
    expect(evidence).not.toHaveTextContent(/degraded|macro-module:|rates_fed/);
  });

  it("expands each asset into reader-facing fact identity, support, and counterevidence", async () => {
    const overview = macroOverviewFixture();
    const spy = overview.asset_presentation.find((asset) => asset.symbol === "SPY");
    if (!spy) throw new Error("SPY fixture is missing");
    spy.horizons = [
      {
        ...spy.horizons[0],
        reader_rationale: {
          text: "证据不足，暂不判断；利率与大类资产条件尚未形成同向确认。",
          origin: "structured_fallback",
        },
      },
      spy.horizons[1],
    ];
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    const actionable = await screen.findByRole("table", { name: "Actionable 资产" });
    const spyRow = within(actionable)
      .getAllByRole("row")
      .find((row) => within(row).queryByRole("cell", { name: "SPY" }));
    if (!spyRow) throw new Error("SPY row did not render");

    expect(within(spyRow).getByText(/理由：证据不足，暂不判断；利率与大类资产/)).toBeVisible();
    expect(within(spyRow).getByText(/理由：持续高实际利率限制中期估值扩张/)).toBeVisible();
    expect(within(spyRow).queryByText("事实动量来源")).toBeNull();
    fireEvent.click(within(spyRow).getByText("查看原因与条件"));
    await waitFor(() => expect(within(spyRow).getAllByText("事实动量来源")).toHaveLength(2));
    expect(within(spyRow).getAllByText("SPY 日线收盘 · 已登记资产数据源")).toHaveLength(2);
    expect(within(spyRow).getAllByText(/截至 2026-07-27/)).toHaveLength(2);
    expect(within(spyRow).getAllByText("支持当前展望")).toHaveLength(2);
    expect(within(spyRow).getAllByText("冲突与反证")).toHaveLength(2);
    expect(within(spyRow).getAllByText("利率与美联储")).toHaveLength(2);
    expect(within(spyRow).getAllByText("信用市场")).toHaveLength(2);
    expect(within(spyRow).getAllByText(/决策主来源 · 参考期 2026-07-27/)).toHaveLength(4);
    expect(within(spyRow).getAllByRole("link", { name: "原始来源" })).toHaveLength(4);
    expect(within(spyRow).getAllByText(/证据不足，暂不判断；利率与大类资产/)).toHaveLength(2);

    const sourceIdentity = within(spyRow).getAllByText("Dataset fixture.spy");
    const evidenceIdentity = within(spyRow).getAllByText(/Evidence ref macro-module:/);
    const machineTokens = within(spyRow).queryAllByText(/\bno_call\b|rates_fed|cross_asset/);
    for (const identity of [...sourceIdentity, ...evidenceIdentity, ...machineTokens]) {
      expect(identity).not.toBeVisible();
    }

    fireEvent.click(within(spyRow).getByText("查看原因与条件"));
    expect(within(spyRow).getAllByText("事实动量来源")).toHaveLength(2);
  });

  it("explains degraded and unavailable health slots and reports disabled backfill honestly", async () => {
    const overview = macroOverviewFixture();
    const credit = overview.modules.find((module) => module.module_id === "credit");
    const volatility = overview.modules.find((module) => module.module_id === "volatility");
    if (!credit || !volatility) throw new Error("module fixtures are missing");
    credit.coverage_state = "partial";
    credit.current_health_state = "degraded";
    credit.coverage_gap_count = 1;
    credit.current_health_gap_count = 1;
    credit.reason = null;
    credit.backfill_execution = {
      state: "paused",
      worker_enabled: false,
      total_targets: 3,
      complete_targets: 1,
      pending_targets: 2,
      failed_targets: 0,
      next_check_at_ms: null,
      reason: {
        code: "history_backfill_worker_disabled",
        message: "历史目标未完成，但回填执行器已停用。",
        impact: "limited",
        affected_dataset_ids: ["fixture.credit"],
        affected_claim_ids: ["claim-rates"],
        retryable: true,
        recovery: "operator_action",
        next_action: "启用回填执行器。",
        next_check_at_ms: null,
      },
    };
    volatility.availability = "unavailable";
    volatility.current_health_state = null;
    volatility.coverage_state = null;
    volatility.history_depth_state = null;
    volatility.backfill_execution = null;
    volatility.reason = {
      code: "macro_module_read_failed",
      message: "波动率模块读取失败。",
      impact: "limited",
      affected_dataset_ids: ["fred.vixcls"],
      affected_claim_ids: ["claim-rates"],
      retryable: true,
      recovery: "automatic",
      next_action: "后台重试波动率模块读取。",
      next_check_at_ms: overview.read_at_ms + 300_000,
    };
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    const health = (await screen.findByRole("heading", { name: "Evidence Health" })).closest("div");
    if (!health) throw new Error("Evidence Health did not render");
    const creditRow = within(health)
      .getByRole("link", { name: "信用市场" })
      .closest<HTMLElement>('[role="row"]');
    const volatilityRow = within(health)
      .getByRole("link", { name: "波动率" })
      .closest<HTMLElement>('[role="row"]');
    if (!creditRow || !volatilityRow) throw new Error("health rows did not render");

    expect(within(creditRow).getByText("降级")).toBeVisible();
    expect(within(creditRow).getByText("该健康状态的结构化解释缺失。")).toBeVisible();
    expect(
      within(creditRow).getByText("影响：未提供影响范围，不能据此扩大或缩小当前判断。"),
    ).toBeVisible();
    expect(within(creditRow).getByText("恢复动作：未提供恢复动作。")).toBeVisible();
    expect(within(creditRow).getByText("历史回填：已暂停")).toBeVisible();
    expect(within(creditRow).getByText("执行器未启用")).toBeVisible();
    expect(within(creditRow).getByText("历史目标未完成，但回填执行器已停用。")).toBeVisible();
    expect(within(creditRow).queryByText(/下次检查/)).toBeNull();

    expect(within(volatilityRow).getByText("模块不可用")).toBeVisible();
    expect(within(volatilityRow).getByText("波动率模块读取失败。")).toBeVisible();
    expect(within(volatilityRow).getByText("影响：限制判断范围")).toBeVisible();
    expect(within(volatilityRow).getByText("恢复动作：后台重试波动率模块读取。")).toBeVisible();
    expect(within(volatilityRow).getByText("历史回填：状态不可用")).toBeVisible();
  });

  it("keeps all twelve unique assets when two server-defined groups are empty", async () => {
    const overview = macroOverviewFixture();
    overview.asset_presentation = overview.asset_presentation.map((asset) => ({
      ...asset,
      group: "actionable",
    }));
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, {
      route: "/macro",
    });

    expect(
      await screen.findByRole("heading", { name: "十二资产：事实动量 vs 条件展望" }),
    ).toBeVisible();
    const actionable = renderedAssetSymbols("Actionable");
    expect(actionable).toHaveLength(12);
    expect(new Set(actionable).size).toBe(12);
    expect(screen.queryByRole("table", { name: "Watch 资产" })).toBeNull();
    expect(screen.queryByRole("table", { name: "Evidence gap 资产" })).toBeNull();
    expectAssetGroupCount("Actionable", 12);
    expectAssetGroupCount("Watch", 0);
    expectAssetGroupCount("Evidence gap", 0);
    expect(screen.getAllByText("本期没有资产归入该组。")).toHaveLength(2);
  });

  it("scopes Live Delta to reader labels and discloses dataset IDs only on demand", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    const heading = await screen.findByRole("heading", {
      name: "主线、矛盾与资产的新增事实",
    });
    const delta = heading.closest("section");
    if (!delta) throw new Error("Live Delta workbench did not render");

    expect(within(delta).getByText("整体主线")).toBeVisible();
    expect(within(delta).getByText("信用利差尚未确认利率压力。")).toBeVisible();
    expect(within(delta).getByText("SPY · 1 周")).toBeVisible();
    expect(within(delta).queryByText(/mld_fixture|claim-rates|falsifier-spy-1w/)).toBeNull();
    expect(
      delta.querySelectorAll('.macro-decision__status-glyph[data-tone="positive"]').length,
    ).toBeGreaterThan(0);

    const auditIds = [
      ...within(delta).getAllByText(/Dataset fred\.bamlh0a0hym2/),
      ...within(delta).getAllByText(/Dataset fred\.dgs2/),
    ];
    const auditMetrics = within(delta).getAllByText(/Metric change_1w_bp/);
    for (const auditId of auditIds) expect(auditId).not.toBeVisible();
    for (const auditMetric of auditMetrics) expect(auditMetric).not.toBeVisible();
    expect(within(delta).getAllByText(/1 周变化/).length).toBeGreaterThan(0);
    expect(within(delta).queryByText(/^change_1w_bp$/)).toBeNull();

    for (const disclosure of within(delta).getAllByText("证据身份")) {
      fireEvent.click(disclosure);
    }
    for (const auditId of auditIds) expect(auditId).toBeVisible();
    for (const auditMetric of auditMetrics) expect(auditMetric).toBeVisible();
  });

  it("shows Outcome benchmark, expiry, and typed pending reasons without machine codes", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    const heading = await screen.findByRole("heading", { name: "Outcome Replay" });
    const outcome = heading.closest("article");
    if (!outcome) throw new Error("Outcome Replay did not render");

    expect(within(outcome).getByText("1 日 · SPY")).toBeVisible();
    expect(within(outcome).getByText("1 周 · SPY")).toBeVisible();
    expect(within(outcome).getByText("1 个月 · SPY")).toBeVisible();
    expect(within(outcome).getAllByText(/等待到期 · 到期/)).toHaveLength(3);
    expect(within(outcome).getAllByText("发布方向：偏空")).toHaveLength(3);
    expect(
      outcome.querySelectorAll('.macro-decision__status-glyph[data-tone="caution"]'),
    ).toHaveLength(3);
    expect(
      outcome.querySelectorAll('.macro-decision__status-glyph[data-tone="negative"]'),
    ).toHaveLength(3);
    expect(within(outcome).getByText("1d 结果观察期尚未结束。")).toBeVisible();
    expect(within(outcome).getByText("1w 结果观察期尚未结束。")).toBeVisible();
    expect(within(outcome).getByText("1m 结果观察期尚未结束。")).toBeVisible();
    expect(within(outcome).getAllByText("恢复动作：到期后按市场数据时钟自动评估。")).toHaveLength(
      3,
    );
    expect(within(outcome).queryByText(/horizon_not_expired|asset_horizon_not_expired/)).toBeNull();
  });

  it("shows the typed cause and recovery path when no publication exists", async () => {
    const overview = macroOverviewFixture();
    overview.thesis = null;
    overview.thesis_state = "missing";
    overview.displayed_session_date = null;
    overview.asset_presentation = [];
    overview.claim_presentation = [];
    overview.live_delta = null;
    overview.outcome_replay = null;
    overview.thesis_reason = {
      code: "macro_thesis_missing",
      message: "该交易日尚无已发布主线。",
      impact: "blocked",
      affected_dataset_ids: [],
      affected_claim_ids: [],
      retryable: true,
      recovery: "automatic",
      next_action: "等待后台发布任务完成。",
      next_check_at_ms: overview.read_at_ms + 300_000,
    };
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    expect(
      await screen.findByRole("heading", { name: "2026-07-27 Macro Thesis 缺失" }),
    ).toBeVisible();
    expect(screen.getByText("该交易日尚无已发布主线。")).toBeVisible();
    expect(screen.getByText("恢复动作：等待后台发布任务完成。")).toBeVisible();
    expect(screen.getByText("Thesis 截点")).toBeVisible();
  });

  it("labels a prior publication as historical fallback without presenting it as current", async () => {
    const overview = macroOverviewFixture();
    overview.session_date = "2026-07-28";
    overview.thesis_state = "not_published";
    overview.thesis_reason = {
      code: "macro_thesis_not_published",
      message: "请求交易日未通过独立审阅。",
      impact: "blocked",
      affected_dataset_ids: [],
      affected_claim_ids: [],
      retryable: false,
      recovery: "operator_action",
      next_action: "检查审阅结果。",
      next_check_at_ms: null,
    };
    overview.displayed_session_date = "2026-07-27";
    overview.fallback = {
      state: "available",
      reason: overview.thesis_reason,
      publication_id: overview.thesis!.publication_id,
      session_date: overview.thesis!.session_date,
      cutoff_ms: overview.thesis!.cutoff_ms,
    };
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    expect(
      await screen.findByRole("heading", {
        name: "请求交易日 2026-07-28 尚未发布，当前展示最近已发布主线 2026-07-27",
      }),
    ).toBeVisible();
    expect(screen.getByText("请求交易日未通过独立审阅。")).toBeVisible();
    expect(screen.getByText(/历史主线截点/)).toBeVisible();
  });

  it("localizes one unavailable module without taking down its route shell", async () => {
    server.use(
      http.get(/.*\/api\/macro\/credit$/, () =>
        HttpResponse.json({
          ok: true,
          data: {
            schema_version: "macro_module_unavailable_v1",
            module_id: "credit",
            label: "信用市场",
            availability: "unavailable",
            reason: {
              code: "macro_module_read_failed",
              message: "信用模块读取失败。",
              impact: "limited",
              affected_dataset_ids: ["fred.bamlh0a0hym2"],
              affected_claim_ids: ["claim-rates"],
              retryable: true,
              recovery: "automatic",
              next_action: "后台将重试模块读取。",
              next_check_at_ms: 1_785_158_700_000,
            },
            href: "/macro/credit",
            thesis_context: {
              state: "missing",
              session_date: null,
              cutoff_ms: null,
              role: null,
              reader_narrative: null,
              claim_ids: [],
              supporting_evidence_refs: [],
              conflicting_evidence_refs: [],
              annotations: [],
              reason: null,
            },
          },
        }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="credit" token="test-token" />, {
      route: "/macro/credit",
    });

    expect(await screen.findByRole("heading", { name: "模块证据暂不可用" })).toBeVisible();
    expect(screen.getByText("信用模块读取失败。")).toBeVisible();
    expect(screen.getByText(/后台将重试模块读取/)).toBeVisible();
    expect(screen.getByRole("link", { name: "主线总览" })).toBeVisible();
  });

  it("surfaces the aggregate typed reason on an available degraded module workbench", async () => {
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("rates fixture mismatch");
    module.status.current_health.state = "degraded";
    module.reason = {
      code: "macro_module_current_degraded",
      message: "利率与美联储仍可读取，但当前事实存在缺口。",
      impact: "limited",
      affected_dataset_ids: ["fixture.dataset"],
      affected_claim_ids: ["claim-rates"],
      retryable: true,
      recovery: "automatic",
      next_action: "等待下一次采集并重新投影该 Dataset。",
      next_check_at_ms: 1_785_158_700_000,
    };
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () => HttpResponse.json({ ok: true, data: module })),
    );
    renderWithProviders(<MacroModulePage moduleId="rates_fed" token="test-token" />, {
      route: "/macro/rates-fed",
    });

    const heading = await screen.findByRole("heading", { name: "模块证据状态需要注意" });
    const notice = heading.closest("section");
    if (!notice) throw new Error("module reason notice did not render");

    expect(within(notice).getByText("利率与美联储仍可读取，但当前事实存在缺口。")).toBeVisible();
    expect(within(notice).getByText("影响：限制判断范围")).toBeVisible();
    expect(within(notice).getByText("受影响事实：核心事实")).toBeVisible();
    expect(within(notice).getByText("受影响论点：1 个论点")).toBeVisible();
    expect(within(notice).getByText("恢复方式：后台自动恢复 · 允许重试")).toBeVisible();
    expect(
      within(notice).getByText("恢复动作：等待下一次采集并重新投影该 Dataset。"),
    ).toBeVisible();
    expect(within(notice).getByText(/下次检查：/)).toBeVisible();
  });

  it("keeps historical Thesis analysis out of the current fact state", async () => {
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("rates fixture mismatch");
    const historicalAnalysis = "这段判断来自上一交易日的已发布主线。";
    module.summary.interpretation = null;
    module.thesis_context = {
      ...module.thesis_context,
      state: "historical",
      session_date: "2026-07-26",
      cutoff_ms: Date.parse("2026-07-26T12:50:00.000Z"),
      reader_narrative: {
        text: historicalAnalysis,
        origin: "publication",
      },
      reason: {
        code: "module_context_uses_prior_publication",
        message: "该模块角色来自最近一份历史 Thesis，不代表 requested session 已发布。",
        impact: "limited",
        affected_dataset_ids: [],
        affected_claim_ids: ["claim-rates"],
        retryable: true,
        recovery: "next_session",
        next_action: "等待 requested session 形成新 publication。",
        next_check_at_ms: null,
      },
    };
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () => HttpResponse.json({ ok: true, data: module })),
    );
    renderWithProviders(<MacroModulePage moduleId="rates_fed" token="test-token" />, {
      route: "/macro/rates-fed",
    });

    const currentLabel = await screen.findByText("CURRENT FACT STATE");
    const currentState = currentLabel.closest("section");
    if (!currentState) throw new Error("current fact state did not render");
    expect(within(currentState).queryByText(historicalAnalysis)).toBeNull();

    const rail = screen.getByLabelText("图表决策注释");
    const historicalContext = rail.querySelector<HTMLElement>('[data-context-state="historical"]');
    if (!historicalContext) throw new Error("historical thesis context did not render");
    expect(within(historicalContext).getByText(historicalAnalysis)).toBeVisible();
    expect(within(historicalContext).getByText("状态：历史主线")).toBeVisible();
    expect(within(historicalContext).getByText("发布交易日：2026-07-26")).toBeVisible();
    expect(within(historicalContext).getByText(/Thesis 截点：/)).toBeVisible();
    expect(
      within(historicalContext).getByText(
        "边界原因：该模块角色来自最近一份历史 Thesis，不代表 requested session 已发布。",
      ),
    ).toBeVisible();
  });

  it("renders source-separated cross-asset facts without identity blending", async () => {
    server.use(
      http.get(/.*\/api\/macro\/cross-asset$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("cross_asset") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="cross_asset" token="test-token" />, {
      route: "/macro/cross-asset",
    });

    expect(await screen.findByRole("heading", { name: "大类资产与期货" })).toBeVisible();
    expect(screen.getByText("证据身份")).toBeInTheDocument();
    expect(screen.getByText("精确来源")).toBeInTheDocument();
    expect(screen.getAllByText("separate_source_facts_no_blend").length).toBeGreaterThan(0);
    expect(
      screen.getByText("展开 Coverage、Current Health、History Depth 与原始事实"),
    ).toBeVisible();
  });

  it("groups normalized cross-asset history into three decision-readable charts", async () => {
    window.location.hash = "#normalized";
    server.use(
      http.get(/.*\/api\/macro\/cross-asset$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("cross_asset") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="cross_asset" token="test-token" />, {
      route: "/macro/cross-asset#normalized",
    });

    expect(await screen.findByRole("heading", { name: "大类资产分组归一化走势" })).toBeVisible();
    expect(screen.getByText("权益")).toBeVisible();
    expect(screen.getByText("久期与信用")).toBeVisible();
    expect(screen.getByText("美元与商品")).toBeVisible();
    expect(screen.queryByText("其他资产")).toBeNull();
  });

  it("renders four credit dimensions and no paid-data placeholder", async () => {
    server.use(
      http.get(/.*\/api\/macro\/credit$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("credit") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="credit" token="test-token" />, {
      route: "/macro/credit",
    });

    expect(await screen.findByText("利差水平与速度")).toBeVisible();
    expect(screen.getAllByText("绝对融资成本").length).toBeGreaterThan(0);
    expect(screen.getByText("银行供给与需求")).toBeVisible();
    expect(screen.getByText("实现信用质量")).toBeVisible();
    expect(screen.queryByText(/TRACE|licensed_unavailable/)).toBeNull();
  });

  it("hard-loads only the hash-selected module category", async () => {
    window.location.hash = "#policy";
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("rates_fed") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="rates_fed" token="test-token" />, {
      route: "/macro/rates-fed#policy",
    });

    expect(await screen.findByRole("heading", { name: "政策走廊与当前市场定价" })).toBeVisible();
    expect(screen.queryByText("名义 Treasury 曲线")).toBeNull();
    expect(screen.getByLabelText("利率与美联储当前视图")).toHaveValue("policy");
    expect(screen.getByLabelText("图表决策注释")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "矛盾" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "失效条件" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "下一检查点" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "覆盖缺口" })).toBeNull();
  });

  it("keeps reconciliation source identities behind an explicit audit disclosure", async () => {
    window.location.hash = "#policy";
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("rates_fed") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="rates_fed" token="test-token" />, {
      route: "/macro/rates-fed#policy",
    });

    expect(await screen.findByRole("heading", { name: "政策走廊与当前市场定价" })).toBeVisible();
    const ledgerHeading = screen.getByRole("heading", { name: "多来源一致性收据" });
    expect(ledgerHeading).not.toBeVisible();

    fireEvent.click(screen.getByText("展开 Coverage、Current Health、History Depth 与原始事实"));
    expect(ledgerHeading).toBeVisible();
    fireEvent.click(screen.getByText("对照 1 · 来源齐全"));

    expect(
      screen.getByText(
        "选择规则：主来源仅用于决策，代理来源独立保留；本次记录 2 个来源、1 组可比较事实。",
      ),
    ).toBeVisible();
    expect(screen.getByText("参考期一致且差异在容差内")).toBeVisible();
    expect(screen.getByText(/差异 0\.02 % · 容差 0\.05/)).toBeVisible();

    const conceptIdentity = screen.getByText("rates_fed.core");
    const proxyIdentity = screen.getByText(/fixture\.proxy · intraday_proxy/);
    expect(conceptIdentity).not.toBeVisible();
    expect(proxyIdentity).not.toBeVisible();
    fireEvent.click(screen.getByText("查看来源身份审计"));
    expect(conceptIdentity).toBeVisible();
    expect(proxyIdentity).toBeVisible();
  });

  it("keeps source clocks, units, formulas, and backfill execution reachable in module evidence", async () => {
    window.location.hash = "#curve";
    const module = macroModuleFixture("rates_fed");
    module.status.backfill_execution = {
      state: "paused",
      worker_enabled: false,
      total_targets: 2,
      complete_targets: 1,
      pending_targets: 1,
      failed_targets: 0,
      next_check_at_ms: null,
      reason: {
        code: "history_backfill_worker_disabled",
        message: "历史回填目标仍未完成，但执行器已停用。",
        impact: "limited",
        affected_dataset_ids: ["fixture.dataset"],
        affected_claim_ids: [],
        retryable: true,
        recovery: "operator_action",
        next_action: "启用回填执行器。",
        next_check_at_ms: null,
      },
    };
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () => HttpResponse.json({ ok: true, data: module })),
    );
    renderWithProviders(<MacroModulePage moduleId="rates_fed" token="test-token" />, {
      route: "/macro/rates-fed#curve",
    });

    await screen.findByRole("heading", { name: "名义 Treasury 曲线" });
    const disclosure = screen.getByText("展开 Coverage、Current Health、History Depth 与原始事实");
    const evidence = disclosure.closest("details");
    if (!evidence) throw new Error("module evidence disclosure did not render");
    fireEvent.click(disclosure);

    expect(within(evidence).getByRole("heading", { name: "计算口径与单位" })).toBeVisible();
    expect(within(evidence).getByText(/收益率曲线水平、斜率与曲率分类/)).toBeVisible();
    expect(within(evidence).getByText(/可读单位/)).toBeVisible();
    expect(within(evidence).getByText("历史回填：已暂停")).toBeVisible();
    expect(within(evidence).getByText("执行器未启用")).toBeVisible();
    expect(within(evidence).queryByText(/下次检查/)).toBeNull();
    expect(within(evidence).getByText(/事实观测/)).toBeVisible();
    expect(within(evidence).getByText(/来源发布/)).toBeVisible();
    expect(within(evidence).getByText(/系统接收/)).toBeVisible();
    expect(within(evidence).getByText("参考期 2026-07-27")).toBeVisible();

    const formulaIdentity = within(evidence).getByText(
      "Formula level_slope_curvature_classification_v2",
    );
    const datasetIdentities = within(evidence).getAllByText("Dataset fixture.dataset");
    const factIdentity = within(evidence).getByText("Fact ref fact:fixture");
    expect(formulaIdentity).not.toBeVisible();
    expect(factIdentity).not.toBeVisible();
    for (const identity of datasetIdentities) expect(identity).not.toBeVisible();

    fireEvent.click(within(evidence).getByText("查看公式版本审计"));
    fireEvent.click(within(evidence).getByText("查看事实技术身份"));
    expect(formulaIdentity).toBeVisible();
    expect(factIdentity).toBeVisible();
  });

  it("omits a next-check time when running backfill has no durable schedule", async () => {
    window.location.hash = "#curve";
    const module = macroModuleFixture("rates_fed");
    module.status.backfill_execution = {
      state: "running",
      worker_enabled: true,
      total_targets: 1,
      complete_targets: 0,
      pending_targets: 1,
      failed_targets: 0,
      next_check_at_ms: null,
      reason: {
        code: "history_backfill_running",
        message: "历史回填执行器正在处理目标。",
        impact: "limited",
        affected_dataset_ids: ["fixture.dataset"],
        affected_claim_ids: [],
        retryable: true,
        recovery: "automatic",
        next_action: "等待当前租约完成后重新读取模块。",
        next_check_at_ms: null,
      },
    };
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () => HttpResponse.json({ ok: true, data: module })),
    );
    renderWithProviders(<MacroModulePage moduleId="rates_fed" token="test-token" />, {
      route: "/macro/rates-fed#curve",
    });

    await screen.findByRole("heading", { name: "名义 Treasury 曲线" });
    const disclosure = screen.getByText("展开 Coverage、Current Health、History Depth 与原始事实");
    const evidence = disclosure.closest("details");
    if (!evidence) throw new Error("module evidence disclosure did not render");
    fireEvent.click(disclosure);

    expect(within(evidence).getByText("历史回填：执行中")).toBeVisible();
    expect(within(evidence).queryByText(/下次检查/)).toBeNull();
  });
});

function renderedAssetSymbols(group: "Actionable" | "Evidence gap" | "Watch"): string[] {
  const table = screen.queryByRole("table", { name: `${group} 资产` });
  if (!table) return [];
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent?.trim() ?? "");
}

function expectAssetGroupCount(
  group: "Actionable" | "Evidence gap" | "Watch",
  count: number,
): void {
  const header = screen.getByRole("heading", { name: group }).closest("header");
  if (!header) throw new Error(`${group} count is not associated with its heading`);
  expect(within(header).getByText(String(count))).toBeVisible();
}
