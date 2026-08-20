import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { newsReviewFixture } from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 命中复盘 (#88). The page's whole job is to make a percentage impossible to read without its denominator,
 * and to keep "not yet" apart from "zero". Every assertion here is one of those two rules.
 */
describe("NewsReviewPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/review$/, () =>
        HttpResponse.json({ ok: true, data: newsReviewFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("reads coverage before accuracy and pairs every rate with its N", async () => {
    renderReview();

    const coverage = await screen.findByLabelText("复盘覆盖率");
    expect(within(coverage).getByText("事件后 1H")).toBeInTheDocument();
    expect(within(coverage).getByText("66.7%")).toBeInTheDocument();
    expect(within(coverage).getByText(/可评估 3 · 已定价 2/)).toBeInTheDocument();
    // The reason a horizon could not be priced is on screen, not folded into the percentage.
    expect(within(coverage).getByText("该时段没有成交 K 线，不做前向填充")).toBeInTheDocument();

    const directions = screen.getByLabelText("方向命中率");
    const bullishRow = within(directions).getByRole("row", { name: /利多/ });
    // The rate and its denominator are one visual unit; a bare percentage is never rendered.
    expect(bullishRow.textContent).toContain("100%");
    expect(bullishRow.textContent).toContain("N=1");
  });

  it("reports neutral judgments beside accuracy rather than inside it", async () => {
    renderReview();

    const directions = await screen.findByLabelText("方向命中率");
    const neutralRow = within(directions).getByRole("row", { name: /中性/ });
    expect(within(neutralRow).getByText("不计入")).toBeInTheDocument();
  });

  it("shows a withheld Event with the rule that withheld it, and never claims causality", async () => {
    renderReview();

    const misses = await screen.findByLabelText("潜在漏推");
    expect(within(misses).getByText("以太坊质押上限调整")).toBeInTheDocument();
    expect(within(misses).getByText("限流")).toBeInTheDocument();
    expect(within(misses).getByText("同一资产窗口内已推送过相似卡片")).toBeInTheDocument();
    // The event-level move and the per-asset move are both shown, and the asset names its own contract.
    expect(within(misses).getAllByText("+9.00%")).toHaveLength(2);
    expect(within(misses).getByText(/binance\.perp:ETHUSDT/)).toBeInTheDocument();
  });

  it("says so when a window has nothing to score instead of rendering zeroes", async () => {
    server.use(
      http.get(/.*\/api\/news\/review$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsReviewFixture({
            coverage: [],
            directions: [],
            event_types: [],
            magnitudes: [],
            potential_misses: [],
            summary: { coverage_1h_pct: null, hit_1h_n: 0, hit_1h_pct: null },
          }),
        }),
      ),
    );
    renderReview();

    expect(await screen.findByText("这个窗口里还没有可复盘的事件。")).toBeInTheDocument();
    expect(screen.getByText("还没有可以打分的方向判断。")).toBeInTheDocument();
    expect(screen.getByText("这个窗口里，没有送达读者又出现明显波动的事件。")).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("keeps the window in the URL and forwards it to the server", async () => {
    const requested: Array<string | null> = [];
    server.use(
      http.get(/.*\/api\/news\/review$/, ({ request }) => {
        requested.push(new URL(request.url).searchParams.get("hours"));
        return HttpResponse.json({ ok: true, data: newsReviewFixture() });
      }),
    );
    renderReview("/news/review?hours=720");

    await waitFor(() => expect(requested).toContain("720"));
    expect(screen.getByRole("combobox")).toHaveValue("720");
  });

  it("publishes the metric version the numbers were computed under", async () => {
    renderReview();

    const details = await screen.findByText("度量口径");
    details.click();
    expect(screen.getByText("reaction_v1")).toBeInTheDocument();
    expect(screen.getByText("事件的 provider 发布时间，不是投递时间")).toBeInTheDocument();
  });
});

function renderReview(path = "/news/review"): ReactNode {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          <NewsPage token="test-token" view="review" />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return null;
}
