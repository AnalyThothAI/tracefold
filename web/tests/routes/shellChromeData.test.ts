import { topbarFigures } from "@routes/shellChromeData";
import { newsStatusFixture } from "@tests/fixtures/newsFixture";
import { tradingStatusFixture } from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

describe("route-aware shell figures", () => {
  const news = newsStatusFixture({
    delivery: {
      ...newsStatusFixture().delivery,
      e2e_p95_ms: 3_400,
      sent_24h: 41,
    },
  });
  const trading = tradingStatusFixture();

  it("identifies the News reading surfaces with delivery facts", () => {
    expect(topbarFigures("/news", news, trading)).toEqual([
      { label: "PUSHED 24H", tone: "accent", value: 41 },
      { label: "E2E P95", text: "3.4 s" },
    ]);
    expect(topbarFigures("/news/events/evt-1", news, trading)).toEqual(
      topbarFigures("/news", news, trading),
    );
  });

  it("uses the telemetry and capital ledgers on the OI monitor", () => {
    const today = Date.parse("2026-08-25T12:00:00Z");
    expect(topbarFigures("/news/oi", news, trading, today)).toEqual([
      { label: "OI FRAMES 24H", tone: "accent", value: 142 },
      { label: "CASES TODAY", value: 9 },
    ]);

    expect(
      topbarFigures(
        "/news/oi",
        news,
        tradingStatusFixture({
          counts: { ...trading.counts, funnel_today: {} },
        }),
        today,
      )[1],
    ).toEqual({ label: "CASES TODAY", value: 0 });
  });

  it("dates an OI case count when the capital ledger stopped before today", () => {
    expect(topbarFigures("/news/oi", news, trading, Date.parse("2026-08-26T00:01:00Z"))[1]).toEqual(
      {
        label: "CASES · 08-25",
        title: "UTC 2026-08-25",
        tone: "caution",
        value: 9,
      },
    );
  });

  it("uses operational latency on the status surface", () => {
    expect(topbarFigures("/news/status", news, trading)).toEqual([
      { label: "EVENTS 24H", tone: "accent", value: 320 },
      { label: "QUEUE P95", text: undefined },
    ]);
  });

  it("keeps paper/live safety facts visible on the trading surface", () => {
    expect(topbarFigures("/trading", news, trading)).toEqual([
      { label: "MODE", text: "PAPER" },
      {
        label: "LIVE READY",
        text: "NOT_APPLICABLE",
        tone: "caution",
      },
      { label: "ORDERS TODAY", text: "3 / 4" },
    ]);
  });
});
