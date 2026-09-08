import { useTradingFactExpiry } from "@features/trading/state/useTradingFactExpiry";
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => vi.useRealTimers());

it("expires retained facts without needing another poll and reschedules a new server budget", () => {
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  const { result, rerender } = renderHook(({ expires }) => useTradingFactExpiry(expires), {
    initialProps: { expires: 2000 },
  });
  expect(result.current).toBe(false);
  act(() => vi.advanceTimersByTime(1001));
  expect(result.current).toBe(true);
  rerender({ expires: 3000 });
  expect(result.current).toBe(false);
  act(() => vi.advanceTimersByTime(1000));
  expect(result.current).toBe(true);
});
