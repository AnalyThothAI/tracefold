import { MacroModulePage, MacroOverviewPage } from "@features/macro";
import type { MacroModuleRouteReadData } from "@features/macro";
import { cleanup, screen, within } from "@testing-library/react";
import { macroModuleFixture, macroOverviewFixture } from "@tests/fixtures/macroFixture";
import { server } from "@tests/msw/server";
import { createTestQueryClient, renderWithProviders } from "@tests/render/renderWithProviders";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

const SESSION_PROPS = {
  bootstrapError: false,
  bootstrapLoading: false,
  token: "test-token",
};

describe("Macro current-fact workbench", () => {
  afterEach(() => cleanup());

  it("renders the six current modules without a Thesis dependency", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );

    renderWithProviders(<MacroOverviewPage {...SESSION_PROPS} />, { route: "/macro" });

    expect(await screen.findByRole("heading", { name: "宏观事实总览" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "当前事实摘要" })).toBeVisible();
    expect(screen.getAllByText("CURRENT FACTS")).toHaveLength(6);
  });

  it("renders server-owned overview transport, quality, availability, gaps, and reasons", async () => {
    const data = macroOverviewFixture();
    const reason = {
      affected_claim_ids: ["rates.current"],
      affected_dataset_ids: ["treasury.daily_nominal_curve"],
      code: "upstream_temporarily_unavailable",
      impact: "blocked" as const,
      message: "利率模块正在等待官方源恢复。",
      next_action: "系统将在下一轮自动检查。",
      next_check_at_ms: Date.parse("2026-07-29T14:00:00Z"),
      recovery: "automatic" as const,
      retryable: false,
    };
    data.transport = {
      last_successful_read_at_ms: Date.parse("2026-07-28T12:40:00Z"),
      reason,
      state: "stale",
    };
    data.data_quality = {
      coverage_gap_count: 2,
      coverage_state: "partial",
      current_health_gap_count: 3,
      current_health_state: "degraded",
      history_depth_state: "insufficient",
      history_gap_count: 4,
    };
    data.modules[0] = {
      ...data.modules[0]!,
      availability: "unavailable",
      coverage_gap_count: 2,
      coverage_state: null,
      current_health_gap_count: 3,
      current_health_state: null,
      history_depth_state: null,
      history_gap_count: 4,
      latest_fact_at_ms: null,
      reason,
      summary: null,
    };
    server.use(http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data })));

    renderWithProviders(<MacroOverviewPage {...SESSION_PROPS} />, { route: "/macro" });

    const overviewStatus = await screen.findByRole("region", { name: "宏观总览状态" });
    expect(within(overviewStatus).getByText("传输状态").nextElementSibling).toHaveTextContent(
      "陈旧",
    );
    expect(within(overviewStatus).getByText("数据覆盖").nextElementSibling).toHaveTextContent(
      "部分 · 2 个缺口",
    );
    expect(within(overviewStatus).getByText("当前质量").nextElementSibling).toHaveTextContent(
      "降级 · 3 个缺口",
    );
    expect(within(overviewStatus).getByText("历史质量").nextElementSibling).toHaveTextContent(
      "不足 · 4 个缺口",
    );
    expect(within(overviewStatus).getByText("upstream_temporarily_unavailable")).toBeVisible();

    const ratesCard = screen.getByRole("heading", { name: "利率与美联储" }).closest("article");
    expect(ratesCard).not.toBeNull();
    expect(within(ratesCard!).getByText("模块可用性").nextElementSibling).toHaveTextContent(
      "不可用",
    );
    expect(within(ratesCard!).getByText("最新事实").nextElementSibling).toHaveTextContent(
      "尚无时间",
    );
    expect(within(ratesCard!).getByText("缺口计数").nextElementSibling).toHaveTextContent(
      "覆盖 2 · 当前 3 · 历史 4",
    );
    expect(within(ratesCard!).getByText("利率模块正在等待官方源恢复。")).toBeVisible();
  });

  it("renders the rates module directly from its persisted read model", async () => {
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("rates_fed") }),
      ),
    );

    renderWithProviders(<MacroModulePage {...SESSION_PROPS} moduleId="rates_fed" />, {
      route: "/macro/rates-fed",
    });

    expect(await screen.findByRole("heading", { name: "利率与美联储" })).toBeVisible();
    expect(screen.getByText(/确定性事实页/)).toBeVisible();
  });

  it("presents server-owned dataset status and recovery before the module workbench", async () => {
    const module = macroModuleFixture("rates_fed");
    module.evidence.dataset_states = [
      {
        concept_id: "policy_rate",
        critical: true,
        current_health: "degraded",
        current_reason: {
          affected_claim_ids: [],
          affected_dataset_ids: ["fred.dff"],
          code: "publication_lag",
          impact: "limited",
          message: "等待下一次官方发布。",
          next_action: "下个发布窗口自动检查。",
          next_check_at_ms: Date.parse("2026-07-29T14:00:00Z"),
          recovery: "next_session",
          retryable: false,
        },
        dataset_id: "fred.dff",
        health_group: "policy",
        history_depth: "not_required",
        history_reason: {
          affected_claim_ids: [],
          affected_dataset_ids: ["fred.dff"],
          code: "history_not_required",
          impact: "none",
          message: "该序列不要求历史回填。",
          recovery: "none",
          retryable: false,
        },
        label: "有效联邦基金利率",
        last_market_at_ms: null,
        latest_received_at_ms: Date.parse("2026-07-28T12:45:00Z"),
        latest_reference: "2026-07-28",
        market_state: "not_applicable",
        next_open_ms: null,
        required_for_current: true,
        required_for_history: false,
        source_role: "official_primary",
        source_state: "healthy",
        source_url: "https://fred.stlouisfed.org/series/DFF",
        trust_tier: "official",
      },
    ];
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () => HttpResponse.json({ ok: true, data: module })),
    );

    renderWithProviders(<MacroModulePage {...SESSION_PROPS} moduleId="rates_fed" />, {
      route: "/macro/rates-fed",
    });

    const diagnostics = await screen.findByRole("region", { name: "数据集状态" });
    expect(
      within(diagnostics)
        .getByText(/数据集审计/)
        .closest("details"),
    ).toHaveAttribute("open");
    const dataset = within(diagnostics)
      .getByRole("heading", { name: "有效联邦基金利率" })
      .closest("article");
    expect(dataset).not.toBeNull();
    const datasetStatus = within(dataset!);
    expect(datasetStatus.getByText("当前健康").nextElementSibling).toHaveTextContent("降级");
    expect(datasetStatus.getByText("当前合同").nextElementSibling).toHaveTextContent("必需");
    expect(datasetStatus.getByText("来源角色").nextElementSibling).toHaveTextContent(
      "official_primary",
    );
    expect(datasetStatus.getByText("来源状态").nextElementSibling).toHaveTextContent("健康");
    expect(datasetStatus.getByText("信任层级").nextElementSibling).toHaveTextContent("官方");
    expect(datasetStatus.getByRole("link", { name: "原始来源" })).toHaveAttribute(
      "href",
      "https://fred.stlouisfed.org/series/DFF",
    );
    expect(datasetStatus.getByText("市场状态").nextElementSibling).toHaveTextContent("不适用");
    expect(datasetStatus.getByText("历史深度").nextElementSibling).toHaveTextContent("不要求");
    expect(datasetStatus.getByText("数据截止").nextElementSibling).toHaveTextContent("2026-07-28");
    expect(datasetStatus.getByText("等待下一次官方发布。")).toBeVisible();
    expect(datasetStatus.getByText(/恢复：下个交易时段/)).toBeVisible();
    expect(datasetStatus.getByText(/下次检查/)).toBeVisible();
    expect(
      diagnostics.compareDocumentPosition(screen.getByRole("tabpanel")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("keeps cached module facts visible and reports a delayed update after refetch failure", async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(["macro", "module", "rates_fed"], macroModuleFixture("rates_fed"));
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: false, error: "refresh unavailable" }, { status: 503 }),
      ),
    );

    renderWithProviders(<MacroModulePage {...SESSION_PROPS} moduleId="rates_fed" />, {
      queryClient,
      route: "/macro/rates-fed",
    });

    expect(await screen.findByRole("heading", { name: "利率与美联储" })).toBeVisible();
    const delayed = await screen.findByRole("status", { name: "Update delayed" });
    expect(delayed).toHaveTextContent("Update delayed");
    expect(delayed).toHaveTextContent("refresh unavailable");
    expect(within(delayed).getByRole("button", { name: "Retry" })).toBeVisible();
  });

  it("renders typed module-unavailable recovery and retry directly from the API", async () => {
    const unavailable: MacroModuleRouteReadData = {
      availability: "unavailable",
      href: "/macro/rates-fed",
      label: "利率与美联储",
      module_id: "rates_fed",
      reason: {
        affected_claim_ids: ["rates.current"],
        affected_dataset_ids: ["treasury.daily_nominal_curve"],
        code: "upstream_temporarily_unavailable",
        impact: "blocked",
        message: "官方利率源暂不可用。",
        next_action: "等待自动恢复，或立即重试读取。",
        next_check_at_ms: Date.parse("2026-07-29T14:00:00Z"),
        recovery: "automatic",
        retryable: true,
      },
      schema_version: "macro_module_unavailable_v1",
    };
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: true, data: unavailable }),
      ),
    );

    renderWithProviders(<MacroModulePage {...SESSION_PROPS} moduleId="rates_fed" />, {
      route: "/macro/rates-fed",
    });

    expect(await screen.findByRole("heading", { name: "模块不可用" })).toBeVisible();
    expect(screen.getByText("官方利率源暂不可用。")).toBeVisible();
    expect(screen.getByText("恢复：自动重试")).toBeVisible();
    expect(screen.getByText(/下次检查/)).toBeVisible();
    expect(screen.getByText("等待自动恢复，或立即重试读取。")).toBeVisible();
    expect(screen.getByText("受影响数据集：treasury.daily_nominal_curve")).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
  });
});
