import { TradingPriceRange } from "@features/trading/ui/TradingPriceRange";
import { render, screen } from "@testing-library/react";
import { tradingCurrentAccountFixture } from "@tests/fixtures/tradingFixture";
import { expect, it } from "vitest";

const position = tradingCurrentAccountFixture().positions![0]!;

it("orders short exit prices numerically and labels a stale mark beyond the range", () => {
  const { container } = render(
    <TradingPriceRange
      stale
      position={{
        ...position,
        side: "short",
        stop_trigger_price: "10200",
        take_profit_trigger_price: "9800",
        mark_price: "10300",
      }}
    />,
  );
  expect(screen.getByText("上次标记 10300")).toBeVisible();
  expect(screen.getByText(/标记价已超出/)).toBeVisible();
  expect(
    Array.from(
      container.querySelectorAll(".trading-price-limits small"),
      (node) => node.textContent,
    ),
  ).toEqual(["止盈触发价", "止损触发价"]);
  expect(container.querySelector(".trading-price-mark")).toHaveAttribute("data-outside", "true");
});

it.each([
  { stop_trigger_price: null },
  { take_profit_trigger_price: null },
  { mark_price: null },
  { mark_price: "not-a-price" },
  { stop_trigger_price: "10200" },
])("does not draw an invented range when facts are incomplete: %j", (overrides) => {
  render(<TradingPriceRange stale={false} position={{ ...position, ...overrides }} />);
  expect(screen.queryByLabelText("已记录的退出价格区间")).toBeNull();
});
