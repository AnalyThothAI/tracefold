import { topbarFigures, topbarNewsSearchParams } from "@routes/shellChromeData";
import { newsStatusFixture } from "@tests/fixtures/newsFixture";
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

  it("identifies the News reading surfaces with delivery facts", () => {
    expect(topbarFigures("/news", news)).toEqual([
      { label: "PUSHED 24H", tone: "accent", value: 41 },
      { label: "E2E P50", text: "2.1 s" },
    ]);
    expect(topbarFigures("/news/events/evt-1", news)).toEqual([
      { label: "PUSHED 24H", tone: "accent", value: 41 },
      { label: "E2E P95", text: "3.4 s" },
    ]);
  });

  it("prints no chrome figure on 市场事实, because status has none to give", () => {
    /*
     * #553 PR-1. The figure here read `pipeline.telemetry_parsed_24h`, which is not a status field any
     * more: market intake is counted off the stored observations and reported per kind by
     * `/api/news/market`, which the page itself leads with. The only ways to keep a figure were a second
     * poll of the page's own endpoint from the frame, or a number nothing publishes.
     */
    expect(topbarFigures("/news/market", news)).toEqual([]);
  });

  it("uses operational latency on the status surface", () => {
    expect(topbarFigures("/news/status", news)).toEqual([
      { label: "EVENTS 24H", tone: "accent", value: 320 },
      { label: "QUEUE P95", text: undefined },
    ]);
  });

  it("prints no trading figure anywhere, on the desk or beside it", () => {
    /*
     * #537 PR-5. The three figures that were here — the lane's last Case clock, the execution mode and
     * the 24 h Signal count — are the first three things `/trading` states on the page itself, and the
     * frame paid for them by polling `/api/trading/status` on every News route every 15 s. The
     * function no longer takes a trading status to print one from.
     */
    expect(topbarFigures("/trading", news)).toEqual([]);
    expect(topbarFigures.length).toBe(2);
    for (const route of ["/news", "/news/market", "/news/status", "/trading"]) {
      for (const figure of topbarFigures(route, news)) {
        expect(figure.label).not.toMatch(/CASE|EXECUTION|SIGNAL|成案/);
      }
    }
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
