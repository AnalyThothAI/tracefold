import { NewsOiTimeline } from "@features/news/ui/market/NewsOiTimeline";
import { render, screen } from "@testing-library/react";
import { newsMarketObservationFixture } from "@tests/fixtures/newsFixture";
import { expect, it } from "vitest";

const sample = (overrides: Parameters<typeof newsMarketObservationFixture>[0] = {}) =>
  newsMarketObservationFixture({
    measurement_contract_status: "proven",
    measurement_definition: "oi_notional_change",
    measurement_window_ms: 900000,
    raw_instrument: "WIFUSDT",
    ...overrides,
  });

it("retains signs and orders discrete observations by their own event time", () => {
  const { container } = render(
    <NewsOiTimeline
      observations={[
        sample({ item_id: "later", event_at_ms: 2000, oi_change_bps: -100 }),
        sample({ item_id: "earlier", event_at_ms: 1000, oi_change_bps: 200 }),
      ]}
    />,
  );
  expect(screen.getByRole("region", { name: "同口径离散 OI 观察" })).toBeVisible();
  expect(
    Array.from(container.querySelectorAll(".news-oi-bar b"), (node) => node.textContent),
  ).toEqual(["+2.00%", "-1.00%"]);
  expect(screen.getByText(/不是连续 OI 行情/)).toBeVisible();
});

it.each([
  { measurement_contract_status: "unproven" as const },
  { measurement_window_ms: 60000 },
  { source_venue: "other" },
  { raw_instrument: "BTCUSDT" },
  { raw_instrument: null },
  { oi_change_bps: null },
  { parse_status: "raw" as const },
])("withholds a comparison for incompatible or missing measurement evidence: %j", (overrides) => {
  render(<NewsOiTimeline observations={[sample(), sample({ item_id: "second", ...overrides })]} />);
  expect(screen.queryByRole("region", { name: "同口径离散 OI 观察" })).toBeNull();
});
