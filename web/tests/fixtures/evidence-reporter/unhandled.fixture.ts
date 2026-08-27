import { expect, test } from "vitest";

test("reports an unhandled rejection", async () => {
  void Promise.reject(new Error("evidence fixture unhandled rejection"));
  await new Promise<void>((resolve) => setImmediate(resolve));
  expect(true).toBe(true);
});
