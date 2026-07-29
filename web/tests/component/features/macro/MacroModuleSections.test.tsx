import { MacroModuleSections } from "@features/macro/ui/MacroModuleSections";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { macroModuleFixture } from "@tests/fixtures/macroFixture";
import { afterEach, describe, expect, it } from "vitest";

const EMPTY_THESIS_CONTEXT = {
  analysis: null,
  annotations: [],
  claim_ids: [],
  conflicting_evidence_refs: [],
  cutoff_ms: null,
  reason: null,
  role: null,
  session_date: null,
  state: "current" as const,
  supporting_evidence_refs: [],
};

describe("macro module evidence workbench", () => {
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", window.location.pathname);
  });

  it("renders only the hash-selected category and projects locatable API changes", () => {
    window.location.hash = "#curve";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("rates fixture mismatch");
    module.thesis_context = EMPTY_THESIS_CONTEXT;
    module.summary.top_changes = [
      {
        ...module.summary.top_changes[0]!,
        as_of: "2026-07-27",
        concept_id: "rates.curve.2s10s",
        dataset_id: "2s10s",
        label: "2s10s 利差走阔",
        value: 35,
      },
    ];

    const { container } = render(<MacroModuleSections module={module} />);

    expect(screen.getByRole("heading", { name: "熊市陡峭化 · 当前 / 1W / 1M / 3M" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "政策走廊与当前市场定价" })).toBeNull();
    expect(
      container.querySelector('[data-annotation-id="change:rates.curve.2s10s:2s10s:2026-07-27"]'),
    ).toHaveAttribute("data-coordinate-value", "35");
    const auditIdentifier = screen.getByText("2s10s");
    expect(auditIdentifier).not.toBeVisible();
    expect(screen.getByText("当前数据 当前 · 历史深度 完整")).toBeVisible();
    expect(screen.queryByText(/current current|history complete/)).toBeNull();
    fireEvent.click(screen.getByText("查看来源审计"));
    expect(auditIdentifier).toBeVisible();
  });

  it("uses stable API condition identities and clamps post-fact thesis annotations", () => {
    window.location.hash = "#curve";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("rates fixture mismatch");
    const cutoff = Date.parse("2026-07-29T08:50:00.000Z");
    const annotations = [
      {
        binding_id: "mainline",
        binding_type: "mainline" as const,
        condition: {
          condition_id: "condition-stable-falsifier",
          dataset_id: "2s10s",
          effect: "invalidation_triggered" as const,
          metric_name: "value_bp",
          module_id: "rates_fed" as const,
          operator: "gte" as const,
          rationale: "期限利差越过失效阈值。",
          threshold: 50,
        },
        kind: "falsifier" as const,
      },
      {
        binding_id: "claim-rates",
        binding_type: "claim" as const,
        condition: {
          condition_id: "condition-stable-confirmation",
          dataset_id: "2s10s",
          effect: "confirming" as const,
          metric_name: "value_bp",
          module_id: "rates_fed" as const,
          operator: "gte" as const,
          rationale: "期限利差确认主线。",
          threshold: 40,
        },
        kind: "confirmation" as const,
      },
    ];
    module.thesis_context = {
      ...module.thesis_context,
      annotations,
      cutoff_ms: cutoff,
    };

    const { container, rerender } = render(<MacroModuleSections module={module} />);

    const expectedIds = [
      "thesis:claim:claim-rates:confirmation:condition-stable-confirmation",
      "thesis:mainline:mainline:falsifier:condition-stable-falsifier",
    ];
    const readIds = () =>
      [...container.querySelectorAll('[data-annotation-id^="thesis:"]')]
        .map((element) => element.getAttribute("data-annotation-id"))
        .sort();
    expect(readIds()).toEqual(expectedIds);
    const falsifier = container.querySelector(
      '[data-annotation-id="thesis:mainline:mainline:falsifier:condition-stable-falsifier"]',
    );
    expect(falsifier).toHaveAttribute("data-coordinate-date", "2026-07-27");
    expect(falsifier).toHaveAttribute("data-source-timestamp", "2026-07-29T08:50:00.000Z");
    expect(falsifier).toHaveAttribute("data-projection", "clamped-end");
    expect(screen.getAllByText(/图上置于末端观测 2026-07-27/)).toHaveLength(3);

    rerender(
      <MacroModuleSections
        module={{
          ...module,
          thesis_context: {
            ...module.thesis_context,
            annotations: [...annotations].reverse(),
          },
        }}
      />,
    );

    expect(readIds()).toEqual(expectedIds);
  });

  it("moves hash tabs with Arrow, Home, and End while keeping focus and selection aligned", async () => {
    window.location.hash = "#curve";
    const module = macroModuleFixture("rates_fed");

    render(<MacroModuleSections module={module} />);

    const curve = screen.getByRole("tab", { name: "收益率曲线" });
    const policy = screen.getByRole("tab", { name: "政策走廊" });
    const positioning = screen.getByRole("tab", { name: "利率仓位" });
    expect(curve).toHaveAttribute("aria-selected", "true");

    curve.focus();
    fireEvent.keyDown(curve, { key: "ArrowRight" });
    await waitFor(() => expect(window.location.hash).toBe("#policy"));
    expect(document.activeElement).toBe(policy);
    await waitFor(() => expect(policy).toHaveAttribute("aria-selected", "true"));

    fireEvent.keyDown(policy, { key: "End" });
    await waitFor(() => expect(window.location.hash).toBe("#positioning"));
    expect(document.activeElement).toBe(positioning);
    await waitFor(() => expect(positioning).toHaveAttribute("aria-selected", "true"));

    fireEvent.keyDown(positioning, { key: "Home" });
    await waitFor(() => expect(window.location.hash).toBe("#curve"));
    expect(document.activeElement).toBe(curve);
    await waitFor(() => expect(curve).toHaveAttribute("aria-selected", "true"));
  });

  it("exposes the selected module chart through a native captioned data table", () => {
    window.location.hash = "#curve";
    const module = macroModuleFixture("rates_fed");

    render(<MacroModuleSections module={module} />);

    fireEvent.click(screen.getAllByText("查看期限数据")[0]!);
    const table = screen.getByRole("table", { name: "名义 Treasury 曲线等价数据表" });
    expect(table).toBeVisible();
    expect(table.querySelector("caption")).toHaveTextContent("名义 Treasury 曲线等价数据表");
  });

  it("renders only the twelve newest official release observations", () => {
    window.location.hash = "#inflation";
    const module = macroModuleFixture("economy_inflation");
    if (module.module_id !== "economy_inflation") {
      throw new Error("economy fixture mismatch");
    }
    const observations = Array.from({ length: 13 }, (_unused, index) => ({
      actual_value: 300 + index,
      estimate_value: 299 + index,
      prior_value: 298 + index,
      published_at_ms: 1_700_000_000_000 + index * 86_400_000,
      received_at_ms: 1_700_000_000_100 + index * 86_400_000,
      reference_period: `P${String(index).padStart(2, "0")}`,
      revised_prior_value: null,
      revision: 0,
      scheduled_at_ms: 1_699_999_000_000 + index * 86_400_000,
      source_url: `https://example.com/releases/P${String(index).padStart(2, "0")}`,
      surprise: 1,
      unit: "index",
    }));
    module.inflation = {
      ...module.inflation,
      official_releases: [
        {
          ...observations.at(-1)!,
          dataset_id: "bls.cpi.release",
          label: "CPI 官方发布",
          observations,
        },
      ],
    };

    render(<MacroModuleSections module={module} />);

    const releases = screen.getByRole("region", { name: "官方发布事实" });
    const articles = within(releases).getAllByRole("article");
    expect(articles).toHaveLength(12);
    expect(articles[0]).toHaveTextContent("P12");
    expect(articles.at(-1)).toHaveTextContent("P01");
    expect(within(releases).queryByText(/P00/)).toBeNull();
    expect(releases).toHaveTextContent("相对预期");
    expect(releases).toHaveTextContent("前值修订");
    expect(releases).not.toHaveTextContent(/\bsurprise\b|\brevision\b/);
  });

  it("omits empty semantic rails instead of manufacturing fallback cards", () => {
    window.location.hash = "#policy";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("rates fixture mismatch");
    module.thesis_context = EMPTY_THESIS_CONTEXT;
    module.summary.top_changes = [];
    module.contradictions = [];
    module.falsifiers = [];
    module.next_checkpoints = [];

    render(<MacroModuleSections module={module} />);

    expect(screen.queryByText("与主线关系")).toBeNull();
    expect(screen.queryByText("关键变化")).toBeNull();
    expect(screen.queryByText("矛盾")).toBeNull();
    expect(screen.queryByText("失效条件")).toBeNull();
    expect(screen.queryByText("下一检查点")).toBeNull();
    expect(screen.queryByText(/尚无足够历史|等待自然频率积累|暂无预设/)).toBeNull();
  });

  it("renders the typed institutional reason and keeps its analysis id in audit disclosure", () => {
    window.location.hash = "#fed";
    const module = macroModuleFixture("rates_fed");
    if (module.module_id !== "rates_fed") throw new Error("rates fixture mismatch");
    module.thesis_context = EMPTY_THESIS_CONTEXT;

    render(<MacroModuleSections module={module} />);

    expect(screen.getByRole("heading", { name: "制度立场、官员分布与事件时间线" })).toBeVisible();
    expect(screen.getByText("机构立场")).toBeVisible();
    expect(screen.getByText("最新已审阅的联邦公开市场委员会声明维持偏鹰立场。")).toBeVisible();
    expect(screen.getByText("fomc-fixture")).not.toBeVisible();
    fireEvent.click(screen.getByText("查看分析审计"));
    expect(screen.getByText("fomc-fixture")).toBeVisible();
    expect(screen.queryByText(/analysis:/)).toBeNull();
  });

  it("obeys the server-owned cross-asset order and never cross-fills source facts", () => {
    window.location.hash = "#returns";
    const module = macroModuleFixture("cross_asset");
    if (module.module_id !== "cross_asset") throw new Error("cross-asset fixture mismatch");
    const spy = {
      ...module.assets.return_matrix[0]!,
      display_order: 2,
      latest_source: {
        ...module.assets.return_matrix[0]!.latest_source,
        fact: null,
      },
      return_source: {
        ...module.assets.return_matrix[0]!.return_source,
        fact: {
          as_of: "2026-07-27",
          change_1d_pct: 1.5,
          change_1m_pct: 3.5,
          change_1w_pct: 2.5,
          dataset_id: module.assets.return_matrix[0]!.return_source.dataset_id,
          latest_value: 999,
          market_time_ms: 1_785_158_400_000,
          source_url: "https://example.com/spy-return",
          unit: "price",
        },
      },
    };
    const qqq = {
      ...module.assets.return_matrix[1]!,
      display_order: 1,
      latest_source: {
        ...module.assets.return_matrix[1]!.latest_source,
        fact: {
          as_of: "2026-07-27",
          change_1d_pct: 88,
          change_1m_pct: null,
          change_1w_pct: null,
          dataset_id: module.assets.return_matrix[1]!.latest_source.dataset_id,
          latest_value: 321,
          market_time_ms: 1_785_158_400_000,
          source_url: "https://example.com/qqq-latest",
          unit: "price",
        },
      },
      return_source: {
        ...module.assets.return_matrix[1]!.return_source,
        fact: null,
      },
    };
    module.assets.return_matrix = [spy, qqq];
    module.assets.source_identity = [
      {
        display_order: 1,
        evidence_kind: "server_identity",
        identity_policy: "separate_source_facts_no_blend",
        label: "服务端指定资产身份",
        selection_policy: "server_exact_no_fallback",
        sources: [
          {
            dataset_id: "server.missing.latest",
            fact: null,
            label: "服务端盘中来源标签",
            source_role: "intraday_proxy",
          },
        ],
        symbol: "SPY",
      },
    ];

    render(<MacroModuleSections module={module} />);

    const matrix = screen.getByRole("table", { name: "十只 ETF 收益矩阵" });
    const rows = within(matrix).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]!.textContent)).toEqual([
      expect.stringContaining("QQQ"),
      expect.stringContaining("SPY"),
    ]);
    const qqqCells = within(rows[0]!).getAllByRole("cell");
    expect(qqqCells[1]).toHaveTextContent("321");
    qqqCells.slice(2).forEach((cell) => expect(cell).toHaveTextContent("—"));
    const spyCells = within(rows[1]!).getAllByRole("cell");
    expect(spyCells[1]).toHaveTextContent("—");
    expect(spyCells[2]).toHaveTextContent("+1.5%");
    expect(spyCells[3]).toHaveTextContent("+2.5%");
    expect(spyCells[4]).toHaveTextContent("+3.5%");
    expect(matrix).not.toHaveTextContent("999");
    expect(matrix).not.toHaveTextContent("88");

    fireEvent.click(screen.getByText("查看来源身份与时钟（1）"));
    expect(screen.getByText("服务端盘中来源标签")).toBeVisible();
    expect(screen.getByText(/server\.missing\.latest · intraday_proxy/)).toBeVisible();
    expect(screen.getByText("尚无来源事实")).toBeVisible();
  });

  it("renders normalized groups and series only from server display order and labels", () => {
    window.location.hash = "#normalized";
    const module = macroModuleFixture("cross_asset");
    if (module.module_id !== "cross_asset") throw new Error("cross-asset fixture mismatch");
    const firstGroup = module.assets.normalized_groups[0]!;
    const secondGroup = module.assets.normalized_groups[1]!;
    module.assets.normalized_groups = [
      {
        ...secondGroup,
        display_order: 2,
        group_id: "server-b",
        label: "服务端组 B",
      },
      {
        ...firstGroup,
        display_order: 1,
        group_id: "server-a",
        label: "服务端组 A",
        series: [
          { ...firstGroup.series[0]!, display_order: 2, label: "服务端系列 二" },
          { ...firstGroup.series[1]!, display_order: 1, label: "服务端系列 一" },
        ],
      },
    ];

    const { container } = render(<MacroModuleSections module={module} />);

    expect(
      [...container.querySelectorAll(".macro-chart figcaption h3")].map(
        (heading) => heading.textContent,
      ),
    ).toEqual(["服务端组 A", "服务端组 B"]);
    expect(
      within(screen.getByLabelText("服务端组 A图例"))
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["服务端系列 一", "服务端系列 二"]);
    expect(screen.queryByRole("heading", { name: "权益" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "久期与信用" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "美元与商品" })).toBeNull();
  });

  it("reads futures only from the canonical return matrix and preserves exact sources", () => {
    window.location.hash = "#futures";
    const module = macroModuleFixture("cross_asset");
    if (module.module_id !== "cross_asset") throw new Error("cross-asset fixture mismatch");
    const es = module.futures.return_matrix[0]!;
    module.futures.return_matrix = [
      {
        ...es,
        latest_source: {
          ...es.latest_source,
          dataset_id: "server.es.intraday",
          label: "服务端 ES 盘中源",
        },
        return_source: {
          ...es.return_source,
          dataset_id: "server.es.daily",
          label: "服务端 ES 日频源",
        },
      },
    ];

    render(<MacroModuleSections module={module} />);

    expect(
      within(screen.getByRole("table", { name: "期货收益矩阵" })).getByText("ES"),
    ).toBeVisible();
    fireEvent.click(screen.getByText("查看收益矩阵精确来源（1）"));
    expect(screen.getByText("服务端 ES 盘中源")).toBeVisible();
    expect(screen.getByText(/server\.es\.intraday · intraday_proxy/)).toBeVisible();
    expect(screen.getByText("服务端 ES 日频源")).toBeVisible();
    expect(screen.getByText(/server\.es\.daily · decision_primary/)).toBeVisible();
  });

  it("renders credit dimension states as reader labels instead of machine enums", () => {
    window.location.hash = "#cycle";
    const module = macroModuleFixture("credit");

    render(<MacroModuleSections module={module} />);

    expect(screen.getByText("正在收紧")).toBeVisible();
    expect(screen.getByText("融资成本偏高")).toBeVisible();
    expect(screen.queryByText("tightening")).toBeNull();
    expect(screen.queryByText("expensive")).toBeNull();
  });
});
