import { NewsQuoteReadState } from "@features/news/ui/chrome/NewsQuoteReadState";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("NewsQuoteReadState", () => {
  afterEach(cleanup);

  it("keeps LKG content, names the failed poll, and does not rewrite the server quote state", () => {
    render(
      <NewsQuoteReadState
        query={{
          data: { quotes: [] },
          dataUpdatedAt: Date.UTC(2026, 7, 28, 6, 30),
          error: new Error("offline"),
          isError: true,
          isFetching: false,
          isLoading: false,
          refetch: vi.fn(),
        }}
      >
        <span data-state="fresh">68,123.40</span>
      </NewsQuoteReadState>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("行情读取失败 · 上次成功于");
    expect(screen.getByText("68,123.40")).toHaveAttribute("data-state", "fresh");
    expect(screen.getByText("68,123.40").closest(".news-quote-read-failed")).toBeInTheDocument();
  });

  it("uses the shared loading and error surfaces when no successful quote batch exists", () => {
    const refetch = vi.fn();
    const { rerender } = render(
      <NewsQuoteReadState
        query={{
          data: undefined,
          dataUpdatedAt: 0,
          error: null,
          isError: false,
          isFetching: true,
          isLoading: true,
          refetch,
        }}
      >
        <span>content</span>
      </NewsQuoteReadState>,
    );
    expect(screen.getByRole("status", { name: "正在读取行情" })).toBeInTheDocument();
    expect(screen.getByText("content")).toBeInTheDocument();

    rerender(
      <NewsQuoteReadState
        query={{
          data: undefined,
          dataUpdatedAt: 0,
          error: new Error("offline"),
          isError: true,
          isFetching: false,
          isLoading: false,
          refetch,
        }}
      >
        <span>content</span>
      </NewsQuoteReadState>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("行情读取失败");
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});
