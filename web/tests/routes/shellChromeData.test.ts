import { topbarFigures, topbarNewsSearchParams } from "@routes/shellChromeData";
import { newsStatusFixture } from "@tests/fixtures/newsFixture";
import { tradingStatusFixture } from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

describe("route-aware shell figures", () => {
  const news = newsStatusFixture({
    delivery: {
      ...newsStatusFixture().delivery,
      e2e_p50_ms: 2_100,
      e2e_p95_ms: 3_400,
      sent_24h: 41,
    },
  });
  const trading = tradingStatusFixture();

  it("identifies the News reading surfaces with delivery facts", () => {
    expect(topbarFigures("/news", news, trading)).toEqual([
      { label: "PUSHED 24H", tone: "accent", value: 41 },
      { label: "E2E P50", text: "2.1 s" },
    ]);
    expect(topbarFigures("/news/events/evt-1", news, trading)).toEqual([
      { label: "PUSHED 24H", tone: "accent", value: 41 },
      { label: "E2E P95", text: "3.4 s" },
    ]);
  });

  it("uses the telemetry and capital ledgers on the OI monitor", () => {
    const today = Date.parse("2026-08-25T12:00:00Z");
    expect(topbarFigures("/news/oi", news, trading, today)).toEqual([
      { label: "PUSHED 24H", tone: "accent", value: 3 },
      { label: "24h 成案 · 今日放行", text: "7 · 1" },
    ]);

    expect(
      topbarFigures(
        "/news/oi",
        news,
        tradingStatusFixture({
          counts: { ...trading.counts, cases_24h: 0 },
        }),
        today,
      )[1],
    ).toEqual({ label: "24h 成案 · 今日放行", text: "0 · 1" });
  });

  it("dates an OI case count when the capital ledger stopped before today", () => {
    expect(topbarFigures("/news/oi", news, trading, Date.parse("2026-08-26T00:01:00Z"))[1]).toEqual(
      {
        label: "成案 · 放行 · 08-25",
        text: "7 · 1",
        title: "UTC 2026-08-25",
        tone: "caution",
      },
    );
  });

  it("uses operational latency on the status surface", () => {
    expect(topbarFigures("/news/status", news, trading)).toEqual([
      { label: "EVENTS 24H", tone: "accent", value: 320 },
      { label: "QUEUE P95", text: undefined },
    ]);
  });

  it("keeps the sole authority and today's entry count visible on the trading surface", () => {
    expect(topbarFigures("/trading", news, trading)).toEqual([
      { label: "AUTHORITY", text: "nautilus" },
      {
        label: "ENGINE",
        text: "READY",
        tone: undefined,
      },
      { label: "今日入场", text: "1" },
    ]);
  });
});

describe("topbar News search scope", () => {
  it("starts a fresh seven-day all-outcome task without hidden feed filters", () => {
    const next = topbarNewsSearchParams("  BTC ETF  ");

    expect(next.toString()).toBe("q=BTC+ETF&outcome=all&hours=168");
    expect([...next.keys()]).toEqual(["q", "outcome", "hours"]);
  });

  it("resets the scope even when an empty draft clears the query", () => {
    expect(topbarNewsSearchParams("  ").toString()).toBe("outcome=all&hours=168");
  });
});
