import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  newsWalletDecimalDetailFixture,
  newsWalletEventDetailFixture,
  newsWalletEventFixture,
  newsWalletEventsFixture,
  newsWalletEventsForParams,
  newsWalletsFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("wallet net-buy events", () => {
  beforeEach(() =>
    server.use(
      http.get(/.*\/api\/news\/wallets\/events$/, ({ request }) =>
        HttpResponse.json({
          ok: true,
          data: newsWalletEventsForParams(new URL(request.url).searchParams),
        }),
      ),
      http.get(/.*\/api\/news\/wallets\/events\/.+$/, () =>
        HttpResponse.json({ ok: true, data: newsWalletEventDetailFixture() }),
      ),
      http.get(/.*\/api\/news\/wallets$/, () =>
        HttpResponse.json({ ok: true, data: newsWalletsFixture() }),
      ),
    ),
  );
  afterEach(cleanup);

  it("reads one row per episode including muted events and opens immutable and current snapshots", async () => {
    renderWallets();
    expect(await screen.findByRole("heading", { name: "聪明钱警报" })).toBeVisible();
    fireEvent.click(await screen.findByRole("link", { name: "XYZ" }));
    expect(
      await screen.findByRole("heading", { name: "合格钱包与买卖净额 · 初始触发快照" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "当前变化与缺口" })).toBeVisible();
    expect(screen.getByText("钱包通知已静音")).toBeVisible();
    expect(screen.getByTestId("location")).toHaveTextContent("episode=");
    expect(screen.queryByText("买入观察")).toBeNull();
    expect(screen.queryByText("摘要")).toBeNull();
  });

  it("opens a shared episode directly when it is absent from the list page", async () => {
    const requests: string[] = [];
    server.use(
      http.get(/.*\/api\/news\/wallets\/events$/, () =>
        HttpResponse.json({ ok: true, data: newsWalletEventsFixture({ events: [] }) }),
      ),
      http.get(/.*\/api\/news\/wallets\/events\/.+$/, ({ request }) => {
        requests.push(new URL(request.url).pathname);
        return HttpResponse.json({ ok: true, data: newsWalletEventDetailFixture() });
      }),
    );
    renderWallets("/news/wallets?episode=" + "a".repeat(64));
    expect(await screen.findByRole("region", { name: "事件详情" })).toBeVisible();
    expect(screen.getByRole("region", { name: "事件详情" })).not.toHaveTextContent(
      /已发送\s*·\s*未发送/,
    );
    expect(requests[0]).toBe("/api/news/wallets/events/" + "a".repeat(64));
    expect(screen.getByText("未取得触发时的可靠价格基准，价格变化保持未知。")).toBeVisible();
    expect(screen.getByText("转出")).toBeVisible();
    expect(screen.queryByText("0.00%")).toBeNull();
  });

  it("keeps zero, negative net flow, tiny prices and known zero changes distinct from unknown", async () => {
    const data = newsWalletDecimalDetailFixture();
    const event = data.event;
    server.use(
      http.get(/.*\/api\/news\/wallets\/events\/.+$/, () => HttpResponse.json({ ok: true, data })),
    );
    renderWallets("/news/wallets?episode=" + event.episode_id);
    fireEvent.click(await screen.findByText("其他观察地址与未纳入原因 · 1"));
    expect(screen.getByText("$-1,500.50")).toBeVisible();
    expect(screen.getAllByText("$0").length).toBeGreaterThan(0);
    expect(screen.getByText("价格 $1.23e-28")).toBeVisible();
    expect(screen.getByText("观察后价格变化 0%")).toBeVisible();
  });

  it("sends history range changes and retains full-scope totals across cursor pages", async () => {
    const requests: URL[] = [];
    server.use(
      http.get(/.*\/api\/news\/wallets\/events$/, ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({
          ok: true,
          data: newsWalletEventsFixture({
            events: [newsWalletEventFixture()],
            totals: { total: 250, active: 20, sent: 12 },
            history_range: "7d",
            next_cursor: url.searchParams.has("cursor") ? null : "next-position",
          }),
        });
      }),
    );
    renderWallets();
    await screen.findByRole("link", { name: "XYZ" });
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(requests.at(-1)?.searchParams.get("history_range")).toBe("7d"));
    fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() => expect(requests.at(-1)?.searchParams.get("cursor")).toBe("next-position"));
    expect(requests.at(-1)?.searchParams.has("to_ms")).toBe(true);
    expect(await screen.findByText(/本页 1 轮 · 全范围 250 轮/)).toBeVisible();
  });

  it("keeps events visible when the independent status read fails, and says so in its own block", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets$/, () =>
        HttpResponse.json({ ok: false, error: "unavailable" }, { status: 503 }),
      ),
    );
    renderWallets();
    expect(await screen.findByRole("link", { name: "XYZ" })).toBeVisible();
    expect(await screen.findByText(/名单 \/ 状态读取失败，事件仍可查阅/)).toBeVisible();
    expect(await statusState()).toBe("query_failed");
  });

  it("keeps the status block readable when the event list is the read that failed", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets\/events$/, () =>
        HttpResponse.json({ ok: false, error: "unavailable" }, { status: 503 }),
      ),
    );
    renderWallets();
    expect(await screen.findByText("质量地址 / 观察地址")).toBeVisible();
    expect(await statusState()).toBe("healthy");
  });

  it("names the production case: one quality address is a list that cannot trigger, not a quiet market", async () => {
    server.use(
      walletsStatus({
        roster: { quality_count: 1, whale_count: 147, supported_quality_count: 1 },
        thresholds: { sufficient: false },
      }),
    );
    renderWallets();
    expect(
      await screen.findByText("当前质量地址 1 个，低于 5m 3 个及 30m 5 个门槛；当前名单不足以触发"),
    ).toBeVisible();
    expect(await statusState()).toBe("roster_insufficient");
    expect(screen.getByRole("region", { name: "名单与采集状态" })).toHaveTextContent("不足以触发");
  });

  it.each([
    [
      "warming_up",
      {
        roster: { quality_count: 6, supported_quality_count: 1 },
        thresholds: { sufficient: false },
      },
      /其中 1 个已具备完整窗口监控支持/,
    ],
    ["collection_lagging", { collection_lagging: true }, /链采集落后于当前时间/],
    ["notifications_disabled", { notifications_enabled: false }, /钱包通知已关闭/],
    [
      "no_match",
      { funnel: { events: 0, intents: 0, sent: 0 } },
      /名单与采集正常，24 小时内没有满足条件的集中净买入。/,
    ],
    [
      "send_failed",
      {
        funnel: {
          events: 3,
          intents: 3,
          sent: 1,
          unsent_reason: "episode_already_reported",
          unsent_reason_count: 2,
        },
      },
      /2 个首报意图没有送达：本轮已首报，不重复发送。/,
    ],
  ])(
    "renders %s with its own sentence rather than one empty list",
    async (state, patch, sentence) => {
      server.use(walletsStatus(patch as Record<string, unknown>));
      renderWallets();
      expect(await screen.findByText(sentence as RegExp)).toBeVisible();
      expect(await statusState()).toBe(state);
    },
  );

  it("reads the collection lag from the server instead of comparing the cutoff with the browser clock", async () => {
    server.use(
      walletsStatus({
        collection_lagging: false,
        tape: { scanned_at_ms: Date.now() - 3_600_000 },
      }),
    );
    renderWallets();
    expect(await screen.findByText(/按已完成的链范围计算/)).toBeVisible();
    expect(await statusState()).toBe("healthy");
    expect(screen.getByRole("region", { name: "名单与采集状态" })).not.toHaveTextContent(
      "采集落后",
    );
  });

  it("reports the roster refresh failure beside the last complete version it kept", async () => {
    server.use(
      walletsStatus({
        roster: { last_error: "robinhoodtrenches:http_429", last_attempt_at_ms: Date.now() },
      }),
    );
    renderWallets();
    expect(
      await screen.findByText(/名单刷新失败 .*：robinhoodtrenches:http_429；保留上一份完整名单。/),
    ).toBeVisible();
  });

  it("distinguishes an empty event range from a failed read", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets\/events$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsWalletEventsFixture({ events: [], totals: { total: 0, active: 0, sent: 0 } }),
        }),
      ),
    );
    renderWallets();
    expect(await screen.findByText("当前历史范围没有集中净买入事件。")).toBeVisible();
    expect(screen.getByRole("button", { name: "7d" })).toBeEnabled();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

function walletsStatus(patch: Record<string, unknown>) {
  const base = newsWalletsFixture();
  const merged = { ...base, ...patch } as Record<string, unknown>;
  for (const key of ["roster", "tape", "thresholds", "funnel"]) {
    if (patch[key])
      merged[key] = { ...(base[key as keyof typeof base] as object), ...(patch[key] as object) };
  }
  return http.get(/.*\/api\/news\/wallets$/, () => HttpResponse.json({ ok: true, data: merged }));
}

async function statusState() {
  return (await screen.findByRole("region", { name: "名单与采集状态" })).getAttribute(
    "data-status-state",
  );
}

function renderWallets(path = "/news/wallets") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          <NewsPage token="test-token" view="wallets" />
          <LocationProbe />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}
