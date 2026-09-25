import { TradingAnalysisDetail } from "@features/trading/ui/TradingAnalysisDetail";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { tradingCaseFixture } from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

it("shows a real WATCH condition and replays the selected failed attempt", async () => {
  const requested: string[] = [];
  server.use(
    http.get(/.*\/api\/trading\/cases\/case-hype\/replay$/, ({ request }) => {
      requested.push(new URL(request.url).searchParams.get("attempt") ?? "latest");
      return HttpResponse.json({
        ok: true,
        data: {
          case_id: "case-hype",
          status: "ok",
          selected_attempt: 1,
          evidence: { knowledge_cutoff_ms: 1000 },
          assessment: { validation_status: "model_schema_invalid" },
          attempts: [],
        },
      });
    }),
  );
  const item = tradingCaseFixture({
    review_mode: "event_wait",
    watch_observation: {
      parent_case_id: "case-hype",
      trigger_id: "trigger-hype",
      condition: {
        kind: "closed_1m_range_cross",
        upper_level: "101",
        lower_level: "99",
        unit: "USDT/base_asset",
      },
      status: "waiting",
      last_observation_status: "not_met",
      last_observed_value: "100",
      last_observed_at_ms: 1000,
      next_check_at_ms: 2000,
      expires_at_ms: 3000,
      created_at_ms: 500,
      updated_at_ms: 1000,
    },
    analysis_attempts: [
      {
        case_id: "case-hype",
        claim_attempt: 1,
        started_at_ms: 900,
        ended_at_ms: 1000,
        analysis_status: "model_schema_invalid",
        physical_call_count: 1,
        known_cost_microusd: 0,
        unknown_cost_calls: 1,
        settled: false,
        error_code: "model_schema_invalid",
        validation_errors: [{ field: "assessment.action", type: "literal_error" }],
        physical_calls: [
          {
            claim_attempt: 1,
            call_index: 0,
            status: "completed",
            started_at_ms: 925,
            finished_at_ms: 975,
            request_ref: "request",
            response_ref: "response",
          },
        ],
      },
    ],
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TradingAnalysisDetail item={item} token="test-token" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.getByText(/相邻 1 分钟收盘首次越过冻结区间/)).toHaveTextContent("101");
  expect(screen.getByText(/未取得结案权/)).toBeVisible();
  expect(screen.getByText(/尝试开始/)).toHaveTextContent(/\.900.*\.000/);
  expect(screen.getByText(/本地开始/)).toHaveTextContent(/\.925.*\.975/);
  screen.getByRole("button", { name: "回放尝试 1" }).click();
  await waitFor(() => expect(requested).toEqual(["1"]));
  const replayCard = screen
    .getByRole("heading", { name: "冻结回放" })
    .closest("section") as HTMLElement;
  expect(await within(replayCard).findByText(/model_schema_invalid/)).toBeVisible();
});

it.each([
  {
    source: { kind: "catalyst", headline: "公开标题", title: "不应使用的别名" },
    expected: "公开标题",
  },
  {
    source: { kind: "catalyst", why: "公开说明", title: "不应使用的别名" },
    expected: "公开说明",
  },
  {
    source: { kind: "catalyst", title: "不应使用的别名" },
    expected: "catalyst",
  },
])("uses public source text ($expected)", async ({ source, expected }) => {
  server.use(
    http.get(/.*\/api\/trading\/cases\/case-hype\/replay$/, () =>
      HttpResponse.json({
        ok: true,
        data: {
          case_id: "case-hype",
          status: "ok",
          source_fact: source,
          evidence: {},
          assessment: {},
          attempts: [],
        },
      }),
    ),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TradingAnalysisDetail item={tradingCaseFixture()} token="test-token" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  screen.getByRole("button", { name: /查看冻结回放/ }).click();
  expect(await screen.findByText(`来源：${expected}`)).toBeVisible();
  expect(screen.queryByText(/不应使用的别名/)).not.toBeInTheDocument();
});
