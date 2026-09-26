import { useTradingFactExpiry } from "@features/trading/state/useTradingFactExpiry";
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => vi.useRealTimers());

it("expires retained facts without needing another poll and reschedules a new server budget", () => {
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  const { result, rerender } = renderHook(
    ({ expires, remaining }) => useTradingFactExpiry(expires, remaining),
    { initialProps: { expires: 2000, remaining: 1000 } },
  );
  expect(result.current).toBe(false);
  act(() => vi.advanceTimersByTime(1001));
  expect(result.current).toBe(true);
  rerender({ expires: 3000, remaining: 1000 });
  expect(result.current).toBe(false);
  act(() => vi.advanceTimersByTime(1001));
  expect(result.current).toBe(true);
});

it("does not renew the same heartbeat after a repeated response or wall-clock jump", () => {
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  const { result, rerender } = renderHook(
    ({ expires, remaining }) => useTradingFactExpiry(expires, remaining),
    { initialProps: { expires: 2000, remaining: 1000 } },
  );
  act(() => vi.advanceTimersByTime(1001));
  expect(result.current).toBe(true);
  rerender({ expires: 2000, remaining: 1000 });
  expect(result.current).toBe(true);
  vi.setSystemTime(61000);
  rerender({ expires: 2000, remaining: 1000 });
  expect(result.current).toBe(true);
  rerender({ expires: 3000, remaining: 1000 });
  expect(result.current).toBe(false);
});
