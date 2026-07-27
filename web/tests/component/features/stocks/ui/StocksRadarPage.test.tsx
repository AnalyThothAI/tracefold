import { StocksRadarPage } from "@features/stocks";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createApiMock, ok, resetApiMock } from "@tests/msw/fixtures";
import { apiHandlers } from "@tests/msw/handlers";
import { server } from "@tests/msw/server";
import { axe } from "jest-axe";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = createApiMock();

beforeEach(() => {
  resetApiMock(apiMock);
  server.use(...apiHandlers(apiMock));
});

afterEach(() => cleanup());

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const controls = {
    onWindowChange: vi.fn(),
  };
  const view = render(
    <QueryClientProvider client={queryClient}>
      <StocksRadarPage token="secret" windowKey="1h" onWindowChange={controls.onWindowChange} />
    </QueryClientProvider>,
  );
  return { ...view, ...controls };
}

describe("StocksRadarPage", () => {
  it("renders PageState table skeleton while stocks radar is loading", async () => {
    let resolveRequest!: () => void;
    const pendingRequest = new Promise<void>((resolve) => {
      resolveRequest = resolve;
    });
    apiMock.readApiImpl = async () => {
      await pendingRequest;
      return ok(emptyStocksRadarResponse());
    };

    const { container } = renderPage();

    const loading = screen.getByRole("status", { name: "loading stocks radar" });
    expect(loading).toHaveClass("page-state-table-skeleton");
    expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument();
    expect(container.querySelector(".stocks-radar-skeleton")).not.toBeInTheDocument();
    resolveRequest();
    await waitFor(() => expect(screen.getByText("No stock flow")).toBeInTheDocument());
  });

  it("loads stocks radar rows and renders partial quote state", async () => {
    apiMock.readApiImpl = async () =>
      ok({
        window: "1h",
        query: {
          window: "1h",
          limit: 48,
          window_start_ms: 1,
          window_end_ms: 2,
        },
        rows: [
          {
            target: {
              target_type: "MarketInstrument",
              target_id: "market_instrument:us_equity:AAPL",
              symbol: "AAPL",
              market: "us_equity",
              exchange: "NASDAQ",
              instrument_type: "equity",
              name: "Apple Inc. Common Stock",
            },
            attention: {
              mentions: 3,
              unique_authors: 2,
              latest_seen_ms: Date.now(),
            },
            latest_event: {
              event_id: "event-aapl",
              author_handle: "toly",
              text: "$AAPL breakout",
              received_at_ms: Date.now(),
            },
            quote: {
              status: "ready",
              price: 291.87,
              reference_close_price: 293.257,
              change_pct: -0.004729,
              asof: "2026-05-12T08:45:45+00:00",
              provider: "yahoo",
              provider_symbol: "AAPL",
              latency_class: "delayed_15m",
              freshness_class: "delayed_15m",
              error: null,
            },
            source_event_ids: ["event-aapl"],
            row_health: [],
          },
          {
            target: {
              target_type: "MarketInstrument",
              target_id: "market_instrument:us_equity:RKLB",
              symbol: "RKLB",
              market: "us_equity",
              exchange: "NASDAQ",
              instrument_type: "equity",
              name: "Rocket Lab USA, Inc.",
            },
            attention: {
              mentions: 1,
              unique_authors: 1,
              latest_seen_ms: Date.now(),
            },
            latest_event: {
              event_id: "event-rklb",
              author_handle: "elonmusk",
              text: "$RKLB launch cadence",
              received_at_ms: Date.now(),
            },
            quote: {
              status: "unavailable",
              price: null,
              reference_close_price: null,
              change_pct: null,
              asof: null,
              provider: null,
              provider_symbol: null,
              latency_class: null,
              freshness_class: null,
              error: "RuntimeError",
            },
            source_event_ids: ["event-rklb"],
            row_health: ["quote_unavailable"],
          },
        ],
        health: {
          returned_count: 2,
          quote_ready_count: 1,
          quote_unavailable_count: 1,
        },
      } as any);

    const { container, onWindowChange } = renderPage();

    expect(await screen.findByRole("heading", { name: "US Stocks" })).toBeInTheDocument();
    expect(await screen.findByLabelText("stock AAPL")).toBeInTheDocument();
    expect(screen.getByLabelText("stock RKLB")).toBeInTheDocument();
    expect(screen.getByText("$291.87")).toBeInTheDocument();
    expect(screen.getByText("RuntimeError")).toBeInTheDocument();
    const controls = screen.getByLabelText("stocks radar controls");
    const windowGroup = within(controls).getByLabelText("radar window");
    expect(within(windowGroup).getByRole("radio", { name: "1h" })).toHaveAttribute(
      "data-state",
      "on",
    );
    expect(within(controls).queryByLabelText("token flow scope")).not.toBeInTheDocument();
    fireEvent.click(within(windowGroup).getByRole("radio", { name: "24h" }));
    expect(onWindowChange).toHaveBeenCalledWith("24h");
    await waitFor(() => {
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/stocks-radar",
        expect.objectContaining({
          token: "secret",
          params: { window: "1h", limit: 48 },
        }),
      );
    });
    expect(await axe(container)).toHaveNoViolations();
  });
});

function emptyStocksRadarResponse() {
  return {
    window: "1h",
    query: {
      window: "1h",
      limit: 48,
      window_start_ms: 1,
      window_end_ms: 2,
    },
    rows: [],
    health: {
      returned_count: 0,
      quote_ready_count: 0,
      quote_unavailable_count: 0,
    },
  } as any;
}
