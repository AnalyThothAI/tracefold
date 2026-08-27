import { expect, test } from "vitest";

test.fails("expected failure must not count as required evidence", () => {
  expect(1).toBe(2);
});
