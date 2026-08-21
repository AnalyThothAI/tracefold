import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NEWS_NOW_MS, newsReviewFixture } from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("NewsReviewPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/review$/, ({ request }) => {
        const view = new URL(request.url).searchParams.get("view");
        if (view === "coverage") {
          return HttpResponse.json({
            ok: true,
            data: newsReviewFixture({
              cohorts: [],
              funnel: {
                accepted: 7,
                external_misses: 2,
                holdout_ready: 0,
                received: 41,
                replayable: 35,
                reviewed: 9,
                total: 100,
              },
              holdout: {
                accepted_case_n: 12,
                accepted_cluster_n: 9,
                case_n: 40,
                cluster_n: 32,
                coverage_interval_95: { lower_pct: 15.6, upper_pct: 45.4 },
                coverage_pct: 28.1,
                status: "insufficient_evidence",
              },
              strata: [],
              tasks: [],
              view: "coverage",
            }),
          });
        }
        if (view === "market") {
          return HttpResponse.json({
            ok: true,
            data: newsReviewFixture({
              disclaimer_zh: "价格变化只是观察证据，不是新闻因果、奖励或 should-push 真值。",
              reaction: {
                coverage: [
                  {
                    coverage_pct: 63,
                    degraded_n: 0,
                    eligible_n: 402,
                    horizon: "1h",
                    horizon_zh: "1 小时",
                    no_primary_n: 0,
                    priced_n: 253,
                    unavailable: [],
                  },
                ],
                directions: [],
                event_types: [],
                magnitudes: [],
                meta: {
                  hours: 168,
                  measured_at_ms: NEWS_NOW_MS,
                  metric_version: "reaction_v1",
                  cohort: "v9/v6/test-model",
                  discovery_window_start_ms: NEWS_NOW_MS - 168 * 3_600_000,
                  window_end_ms: NEWS_NOW_MS,
                  window_start_ms: NEWS_NOW_MS - 168 * 3_600_000,
                },
                potential_misses: [
                  {
                    asset_n: 1,
                    assets: [],
                    decision_zh: "未推送",
                    direction_zh: "",
                    event_id: "wmt-a",
                    event_type_zh: "",
                    fact_cluster_key: "a".repeat(64),
                    fact_cluster_n: 4,
                    final_decision: "drop",
                    headline_zh: "沃尔玛下调全年业绩指引",
                    leader_title: "Walmart lowers full-year guidance",
                    magnitude_zh: "",
                    opened_at_ms: NEWS_NOW_MS - 6 * 3_600_000,
                    override_rule_zh: "",
                    related_event_ids: ["wmt-a", "wmt-b", "wmt-c", "wmt-d"],
                    return_1h_bps: -692,
                    return_4h_bps: -500,
                    storyline_key: "asset:WMT",
                    throttled_by_zh: "",
                  },
                ],
                summary: { coverage_1h_pct: null, hit_1h_n: 0, hit_1h_pct: null },
              },
              tasks: [],
              title_zh: "事后市场观察",
              view: "market",
            }),
          });
        }
        return HttpResponse.json({ ok: true, data: newsReviewFixture() });
      }),
      http.get(/.*\/api\/news\/review\/tasks\/.+\/evidence$/, () =>
        HttpResponse.json({
          ok: true,
          data: {
            accepted_review: null,
            agent: {
              final_decision: "drop",
              verdict: { headline_zh: "DRAM 合约价继续上涨", why_zh: "价格上涨改善厂商议价能力。" },
            },
            disclosure: { dataset_role: "discovery", outcome_revealed: true, pairing: "unpaired" },
            evidence: {
              focus_fact: {
                context: "August 1-20 preliminary data",
                text: "South Korea DRAM export unit price continued to rise",
              },
            },
            market_reactions: [],
            reader_receipt: { state: null, truth: "not_received", truth_zh: "未送达" },
            rubric: {},
            task: newsReviewFixture().tasks?.[0],
            versions: {},
          },
        }),
      ),
    );
  });

  afterEach(cleanup);

  it("opens an evidence-bound event rubric instead of copying a label command", async () => {
    renderReview();
    expect(await screen.findByText("学习复盘")).toBeInTheDocument();
    fireEvent.click(
      await screen.findByText("South Korea DRAM export unit price continued to rise"),
    );
    expect(await screen.findByText("模型当时看到的事实")).toBeInTheDocument();
    expect(screen.getByText("未送达")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交并下一条" })).toBeInTheDocument();
    expect(screen.queryByText(/复制.*标注命令/)).not.toBeInTheDocument();
  });

  it("shows coverage counts as evidence coverage rather than model accuracy", async () => {
    renderReview("/news/review?view=coverage");
    expect(await screen.findByText("复盘证据漏斗")).toBeInTheDocument();
    expect(screen.getByText("41")).toBeInTheDocument();
    expect(screen.getByText("35")).toBeInTheDocument();
    expect(screen.getByText("百分比只表示人工证据覆盖，不是模型准确率")).toBeInTheDocument();
    expect(screen.getByText(/目前只有 9 个已接受独立事实簇/)).toBeInTheDocument();
    expect(screen.getByText(/95% 区间 15.6%–45.4%/)).toBeInTheDocument();
  });

  it("posts a typed evidence-bound rubric with concurrency and idempotency headers", async () => {
    let submitted: Record<string, unknown> | null = null;
    let submittedHeaders: Record<string, string | null> | null = null;
    server.use(
      http.post(/.*\/api\/news\/review\/tasks\/.+\/responses$/, async ({ request }) => {
        submitted = (await request.json()) as Record<string, unknown>;
        submittedHeaders = {
          authorization: request.headers.get("authorization"),
          contentType: request.headers.get("content-type"),
          idempotencyKey: request.headers.get("idempotency-key"),
          ifMatch: request.headers.get("if-match"),
        };
        return HttpResponse.json({
          ok: true,
          data: {
            idempotent: false,
            next_task: null,
            receipt: { acceptance_id: "accept-1", review_id: "review-1" },
            updated_queue_counts: { pending: 0 },
          },
        });
      }),
    );
    renderReview();
    fireEvent.click(
      await screen.findByText("South Korea DRAM export unit price continued to rise"),
    );
    await screen.findByText("模型当时看到的事实");
    fireEvent.change(screen.getByLabelText("应该推送"), { target: { value: "must_push" } });
    fireEvent.change(screen.getByLabelText("事实忠实"), { target: { value: "fail" } });
    fireEvent.change(screen.getByLabelText("第一责任环节"), {
      target: { value: "triage_prompt" },
    });
    fireEvent.change(screen.getByLabelText("说明或期望修正"), {
      target: { value: "不要声称已被预期覆盖" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交并下一条" }));

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      kind: "event_rubric",
      should_push: "must_push",
      dimensions: { factual_fidelity: "fail" },
      evidence_refs: ["source:focus_fact", "agent:output"],
      expected_correction: "不要声称已被预期覆盖",
      first_bad_owner: "triage_prompt",
    });
    expect(submittedHeaders).toMatchObject({
      authorization: "Bearer test-token",
      contentType: "application/json",
      idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
      ifMatch: `"${"a".repeat(64)}"`,
    });
  });

  it("submits a blind pairwise preference without revealing either arm", async () => {
    let submitted: Record<string, unknown> | null = null;
    const baseTask = newsReviewFixture().tasks?.[0];
    if (!baseTask) throw new Error("review fixture must contain one event task");
    const pairTask = {
      ...baseTask,
      agent_headline: null,
      agent_why: null,
      event_id: null,
      final_decision: null,
      headline: null,
      mode: "pairwise" as const,
      reader_receipt: null,
      task_id: `pair.${"b".repeat(64)}.${"c".repeat(64)}`,
    };
    server.use(
      http.get(/.*\/api\/news\/review$/, ({ request }) => {
        const mode = new URL(request.url).searchParams.get("mode");
        return HttpResponse.json({
          ok: true,
          data:
            mode === "pairwise"
              ? newsReviewFixture({ mode: "pairwise", tasks: [pairTask] })
              : newsReviewFixture(),
        });
      }),
      http.get(/.*\/api\/news\/review\/tasks\/.+\/evidence$/, () =>
        HttpResponse.json({
          ok: true,
          data: {
            accepted_review: null,
            disclosure: {
              arm_identity_revealed: false,
              outcome_revealed: false,
              pairing: "paired",
            },
            output_A: { headline_zh: "输出 A", why_zh: "A 的理由" },
            output_B: { headline_zh: "输出 B", why_zh: "B 的理由" },
            task: pairTask,
          },
        }),
      ),
      http.post(/.*\/api\/news\/review\/tasks\/.+\/responses$/, async ({ request }) => {
        submitted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          ok: true,
          data: {
            idempotent: false,
            next_task: null,
            receipt: { acceptance_id: "accept-pair", review_id: "review-pair" },
            updated_queue_counts: { pending: 0 },
          },
        });
      }),
    );

    renderReview();
    fireEvent.click(await screen.findByRole("button", { name: "匿名 A/B" }));
    fireEvent.click(await screen.findByText("匿名输出 A / B"));
    expect(await screen.findByText("输出 A")).toBeInTheDocument();
    expect(screen.queryByText(/stable|candidate/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("更好的输出"), { target: { value: "A" } });
    fireEvent.click(screen.getByLabelText("A 有无证据事实"));
    fireEvent.click(screen.getByRole("button", { name: "提交匿名比较" }));
    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toEqual({
      critical_errors: ["A:unsupported_fact"],
      evidence_refs: ["output:A", "output:B"],
      kind: "blind_pairwise",
      note: "",
      preference: "A",
    });
  });

  it("records an external miss without allowing client-owned provenance", async () => {
    let submitted: Record<string, unknown> | null = null;
    server.use(
      http.post(/.*\/api\/news\/review\/external-misses$/, async ({ request }) => {
        submitted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          ok: true,
          data: {
            idempotent: false,
            next_task: null,
            receipt: { acceptance_id: "accept-miss", review_id: "review-miss" },
            updated_queue_counts: {},
          },
        });
      }),
    );
    renderReview();
    await screen.findByText("外部漏召回");
    fireEvent.change(screen.getByLabelText("标题"), { target: { value: "重大外部事实" } });
    fireEvent.change(screen.getByLabelText("来源 URL"), {
      target: { value: "https://example.test/primary" },
    });
    fireEvent.change(screen.getByLabelText("正文摘录"), { target: { value: "原始来源正文" } });
    fireEvent.click(screen.getByRole("button", { name: "记录漏召回" }));
    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      body: "原始来源正文",
      kind: "external_miss",
      source_url: "https://example.test/primary",
      title: "重大外部事实",
      rubric: {
        dimensions: { timeliness: "fail" },
        should_push: "must_push",
      },
    });
    expect(submitted).not.toHaveProperty("provenance");
  });

  it.each([
    [409, "news_review_idempotency_conflict", "这次提交编号已被另一份内容使用"],
    [503, "review_write_unavailable", "新闻读取和线上推送不受影响"],
  ])(
    "explains mutation error %s without implying the News hot path failed",
    async (status, code, copy) => {
      server.use(
        http.post(/.*\/api\/news\/review\/tasks\/.+\/responses$/, () =>
          HttpResponse.json({ ok: false, error: code }, { status }),
        ),
      );
      renderReview();
      fireEvent.click(
        await screen.findByText("South Korea DRAM export unit price continued to rise"),
      );
      await screen.findByText("模型当时看到的事实");
      fireEvent.click(screen.getByRole("button", { name: "提交并下一条" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(copy);
    },
  );

  it("keeps price in a non-causal evidence view", async () => {
    renderReview("/news/review?view=market");
    expect(await screen.findByText("事后市场观察")).toBeInTheDocument();
    expect(screen.getByText(/不是新闻因果、奖励/)).toBeInTheDocument();
    expect(screen.getByText(/不能证明因果/)).toBeInTheDocument();
    expect(screen.getByText("253 / 402 个成熟事件有价格")).toBeInTheDocument();
    expect(screen.getByText("4 个 Event / 1 个事实")).toBeInTheDocument();
    expect(screen.getByText("沃尔玛下调全年业绩指引")).toBeInTheDocument();
    expect(screen.queryByText(/HIT 1H/)).not.toBeInTheDocument();
  });

  it("keeps the selected window in the request", async () => {
    const requested: Array<string | null> = [];
    server.use(
      http.get(/.*\/api\/news\/review$/, ({ request }) => {
        requested.push(new URL(request.url).searchParams.get("hours"));
        return HttpResponse.json({ ok: true, data: newsReviewFixture() });
      }),
    );
    renderReview("/news/review?hours=720");
    await waitFor(() => expect(requested).toContain("720"));
    expect(screen.getByRole("combobox", { name: "复盘窗口" })).toHaveValue("720");
    fireEvent.click(screen.getByRole("button", { name: "市场旁证" }));
    await waitFor(() => expect(requested).toContain("168"));
    expect(screen.getByRole("combobox", { name: "复盘窗口" })).toHaveValue("168");
    expect(screen.queryByRole("option", { name: "30 天" })).not.toBeInTheDocument();
  });
});

function renderReview(path = "/news/review"): ReactNode {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column" data-now={NEWS_NOW_MS}>
          <NewsPage token="test-token" view="review" />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return null;
}
