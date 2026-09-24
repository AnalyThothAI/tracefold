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
      condition: {
        kind: "closed_1m_price_crosses",
        operator: "gte",
        level: "101",
        unit: "USDT/base_asset",
      },
      status: "pending",
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
        ended_at_ms: 1000,
        analysis_status: "model_schema_invalid",
        physical_call_count: 1,
        settled: false,
        error_code: "model_schema_invalid",
        validation_errors: [{ field: "assessment.action", type: "literal_error" }],
        physical_calls: [
          { claim_attempt: 1, call_index: 0, request_ref: "request", response_ref: "response" },
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
  expect(screen.getByText(/收盘 1 分钟价格越过冻结价位/)).toHaveTextContent("101");
  expect(screen.getByText(/未取得结案权/)).toBeVisible();
  screen.getByRole("button", { name: "回放尝试 1" }).click();
  await waitFor(() => expect(requested).toEqual(["1"]));
  const replayCard = screen
    .getByRole("heading", { name: "冻结回放" })
    .closest("section") as HTMLElement;
  expect(await within(replayCard).findByText(/model_schema_invalid/)).toBeVisible();
});
