import { expect, test } from "vitest";

test.fails("reports an expected failure that unexpectedly passes", () => {
  expect(true).toBe(true);
});
