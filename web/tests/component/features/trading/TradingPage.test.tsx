import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  TRADING_NOW_MS,
  tradingCaseFixture,
  tradingCasesFixture,
  tradingCommandFixture,
  tradingCommandsFixture,
  tradingCurrentAccountFixture,
  tradingExecutionFixture,
  tradingObservationFixture,
  tradingObservationsFixture,
  tradingSignalFixture,
  tradingSignalsFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("TradingPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({ ok: true, data: tradingCasesFixture() }),
      ),
      http.get(/.*\/api\/trading\/signals$/, () =>
        HttpResponse.json({ ok: true, data: tradingSignalsFixture() }),
      ),
      http.get(/.*\/api\/trading\/execution\/observations$/, () =>
        HttpResponse.json({ ok: true, data: tradingObservationsFixture() }),
      ),
      http.get(/.*\/api\/trading\/execution\/commands$/, () =>
        HttpResponse.json({ ok: true, data: tradingCommandsFixture() }),
      ),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the execution truth boundary without claiming disabled execution readiness", async () => {
    renderTrading();

    expect(await screen.findByRole("heading", { name: "Trading Desk" })).toBeVisible();
    expect(screen.getAllByText("disabled").length).toBeGreaterThan(0);
    expect(screen.getAllByText("当前 24 小时窗口没有 Command。")).toHaveLength(2);
    expect(screen.getByText("NOT PROVEN")).toBeVisible();
    expect(screen.getByText(/控制已锁定/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Pause entries" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resume / Arm" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Flatten account" })).toBeDisabled();
  });

  it("shows a Case and its separately read Signal with the same identity", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({
            cases: [
              tradingCaseFixture({
                base_symbol: "SOL",
                case_id: "case-sol",
                market_key: "crypto:perp:SOL:USDT",
                policy_decision: "long",
                policy_reason: "smart_money_momentum_long",
                state: "SIGNAL_EMITTED",
                underlying_key: "crypto:SOL",
              }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/signals$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingSignalsFixture({ signals: [tradingSignalFixture({ case_id: "case-sol" })] }),
        }),
      ),
    );

    renderTrading();

    expect((await screen.findAllByText("crypto:perp:SOL:USDT")).length).toBeGreaterThan(0);
    expect(screen.getByText("已发出 Signal")).toBeVisible();
    expect(screen.getAllByText("PERSISTED").length).toBeGreaterThan(0);
  });

  it("does not turn a Signal read failure into an empty ledger", async () => {
    server.use(
      http.get(/.*\/api\/trading\/signals$/, () =>
        HttpResponse.json({ ok: false, error: "signal_unavailable" }, { status: 503 }),
      ),
    );

    renderTrading();

    expect(await screen.findByText("Signal 账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.queryByText("当前 24 小时窗口没有 Signal。")).toBeNull();
  });

  it("renders durable execution observations without inferring an order state", async () => {
    server.use(
      http.get(/.*\/api\/trading\/execution\/observations$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingObservationsFixture({ observations: [tradingObservationFixture()] }),
        }),
      ),
    );

    renderTrading();

    fireEvent.click(await screen.findByText("Advanced Audit"));
    expect(screen.getByText("signal_disposition")).toBeVisible();
    expect(screen.getAllByText("demo-v1").length).toBeGreaterThan(0);
  });

  it("marks later execution stages unknown when the Observation ledger cannot be read", async () => {
    server.use(
      http.get(/.*\/api\/trading\/execution\/observations$/, () =>
        HttpResponse.json({ ok: false, error: "observation_unavailable" }, { status: 503 }),
      ),
      http.get(/.*\/api\/trading\/execution\/commands$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCommandsFixture({ commands: [tradingCommandFixture()] }),
        }),
      ),
    );

    renderTrading();

    expect(
      await screen.findByText(
        "Observation 账本读取失败；不能断言 Runtime、venue、fill 或完成状态。",
      ),
    ).toBeVisible();
    expect(screen.getAllByText("PERSISTED").length).toBeGreaterThan(0);
  });

  it("renders an awaiting intent without manufacturing a Runtime disposition", async () => {
    server.use(
      http.get(/.*\/api\/trading\/execution\/commands$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCommandsFixture({ commands: [tradingCommandFixture()] }),
        }),
      ),
    );

    renderTrading();

    expect((await screen.findAllByText("Pause entries")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("PERSISTED").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Runtime 受理").length).toBeGreaterThan(0);
    expect(screen.getByText(/HTTP 200 只证明意图已持久化/)).toBeVisible();
  });

  it("shows current risk, position protection, and order uncertainty without inferring flat", async () => {
    const position = tradingCurrentAccountFixture().positions![0]!;
    const account = tradingCurrentAccountFixture({
      complete: false,
      unknown_orders_count: 1,
      positions: [
        {
          ...position,
          owned: false,
          protection_full_coverage: false,
          protection_quantity: null,
          protection_status: "pending",
          protection_trigger_price: null,
        },
      ],
    });
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingExecutionFixture({
              account_flat: false,
              account_flat_proven: false,
              alive: true,
              credential_fingerprint: "credential-fingerprint-test",
              current_account: account,
              entries_armed: false,
              entries_paused: true,
              entry_block_reason: "entries_paused",
              execution_safe: true,
              image_digest: `sha256:${"a".repeat(64)}`,
              mode: "paper",
              open_orders_count: 1,
              positions_count: 1,
              protection_status: "pending",
              runtime_release: "nautilus-1.231.0+oi-v1",
            }),
          }),
        }),
      ),
    );

    renderTrading();

    const safety = await screen.findByLabelText("执行安全状态");
    expect(within(safety).getAllByText("YES")).toHaveLength(2);
    expect(within(safety).getByText("NOT PROVEN")).toBeVisible();
    expect(screen.getByText("$997.50")).toBeVisible();
    expect(screen.getByText("PARTIAL")).toBeVisible();
    expect(screen.getAllByText("PROTECTION PENDING").length).toBeGreaterThan(0);
    expect(screen.getByText("UNCLAIMED")).toBeVisible();
    expect(screen.getByText("NOT FULLY COVERED")).toBeVisible();
    expect(screen.getByText("REDUCE ONLY", { exact: false })).toBeVisible();
    expect(screen.getByText("stop-order-1")).not.toBeVisible();
    expect(screen.getByText("credential-fingerprint-test")).not.toBeVisible();

    fireEvent.click(screen.getByText("Advanced Audit"));
    expect(screen.getByText("stop-order-1")).toBeVisible();
    expect(screen.getByText("credential-fingerprint-test")).toBeVisible();
  });

  it("expires a cached flat proof and restores the Flatten safety command", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(TRADING_NOW_MS);
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingExecutionFixture({
              account_flat: true,
              account_flat_proven: true,
              alive: true,
              entries_armed: false,
              entries_paused: true,
              entry_block_reason: "entries_paused",
              execution_safe: true,
              mode: "paper",
              reconciliation_age_ms: 9_000,
            }),
            measured_at_ms: TRADING_NOW_MS,
          }),
        }),
      ),
    );
    renderTrading();

    expect(await screen.findByText("PROVEN")).toBeVisible();
    expect(screen.getByRole("button", { name: "Flatten account" })).toBeDisabled();

    now.mockReturnValue(TRADING_NOW_MS + 1_001);
    fireEvent.click(screen.getByText("HYPE"));

    expect(screen.getByText("NOT PROVEN")).toBeVisible();
    expect(screen.getByRole("button", { name: "Flatten account" })).toBeEnabled();
  });

  it("confirms resume before posting a stable intent and labels success as persistence only", async () => {
    vi.stubGlobal("crypto", {
      randomUUID: () => "11111111-1111-4111-8111-111111111111",
    });
    let posted: unknown;
    let authorization: string | null = null;
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingExecutionFixture({
              alive: true,
              entries_armed: false,
              entries_paused: true,
              entry_block_reason: "entries_paused",
              execution_safe: true,
              mode: "paper",
            }),
          }),
        }),
      ),
      http.post(/.*\/api\/trading\/execution\/commands$/, async ({ request }) => {
        authorization = request.headers.get("authorization");
        posted = await request.json();
        return HttpResponse.json({
          ok: true,
          data: {
            command_id: "a".repeat(64),
            disposition: "awaiting_runtime",
            reason: null,
            requested_at_ns: 1,
            seq: 7,
            truth: "intent_recorded_not_runtime_or_venue",
          },
        });
      }),
    );
    renderTrading();

    fireEvent.click(await screen.findByRole("button", { name: "Resume / Arm" }));
    expect(screen.getByRole("alertdialog")).toBeVisible();
    expect(posted).toBeUndefined();
    fireEvent.click(screen.getByRole("button", { name: "确认写入 Command" }));

    await waitFor(() => expect(posted).toBeDefined());
    expect(authorization).toBe("Bearer test-token");
    expect(posted).toMatchObject({
      request_id: "11111111-1111-4111-8111-111111111111",
      text: "/resume operator console CONFIRM",
    });
    expect(await screen.findByText(/Command 已持久化/)).toHaveTextContent(
      "这不代表 Runtime 受理、订单或成交",
    );
  });

  it("requires typed confirmation before writing a Live command", async () => {
    vi.stubGlobal("crypto", {
      randomUUID: () => "22222222-2222-4222-8222-222222222222",
    });
    let posted = false;
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingExecutionFixture({
              alive: true,
              entries_armed: false,
              entries_paused: true,
              entry_block_reason: "entries_paused",
              execution_safe: true,
              mode: "live",
            }),
          }),
        }),
      ),
      http.post(/.*\/api\/trading\/execution\/commands$/, () => {
        posted = true;
        return HttpResponse.json({
          ok: true,
          data: {
            command_id: "b".repeat(64),
            disposition: "awaiting_runtime",
            reason: null,
            requested_at_ns: 1,
            seq: 8,
            truth: "intent_recorded_not_runtime_or_venue",
          },
        });
      }),
    );
    renderTrading();

    fireEvent.click(await screen.findByRole("button", { name: "Resume / Arm" }));
    const submit = screen.getByRole("button", { name: "确认写入 Command" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Live 模式：输入 CONFIRM 进行二次确认"), {
      target: { value: "CONFIRM" },
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(posted).toBe(true));
  });

  it("reuses the exact command envelope after an unknown submission result", async () => {
    let uuidCalls = 0;
    vi.stubGlobal("crypto", {
      randomUUID: () => {
        uuidCalls += 1;
        return "33333333-3333-4333-8333-333333333333";
      },
    });
    const bodies: unknown[] = [];
    let attempts = 0;
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingExecutionFixture({
              alive: true,
              entries_armed: false,
              entries_paused: true,
              entry_block_reason: "entries_paused",
              execution_safe: true,
              mode: "paper",
            }),
          }),
        }),
      ),
      http.post(/.*\/api\/trading\/execution\/commands$/, async ({ request }) => {
        bodies.push(await request.json());
        attempts += 1;
        if (attempts === 1) {
          return HttpResponse.json({ ok: false, error: "service_busy" }, { status: 503 });
        }
        return HttpResponse.json({
          ok: true,
          data: {
            command_id: "c".repeat(64),
            disposition: "awaiting_runtime",
            reason: null,
            requested_at_ns: 1,
            seq: 9,
            truth: "intent_recorded_not_runtime_or_venue",
          },
        });
      }),
    );
    renderTrading();

    fireEvent.click(await screen.findByRole("button", { name: "Resume / Arm" }));
    fireEvent.click(screen.getByRole("button", { name: "确认写入 Command" }));
    expect(await screen.findByText(/提交结果未知/)).toHaveTextContent(
      "复用同一 request ID、时钟和文本",
    );

    fireEvent.click(screen.getByRole("button", { name: "Resume / Arm" }));
    fireEvent.click(screen.getByRole("button", { name: "确认写入 Command" }));
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(uuidCalls).toBe(1);
    expect(bodies[1]).toEqual(bodies[0]);
  });
});

function renderTrading() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <TradingPage token="test-token" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}
