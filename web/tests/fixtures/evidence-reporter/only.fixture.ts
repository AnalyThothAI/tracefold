import { expect, test } from "vitest";

test.only("reports an only test", () => {
  expect(true).toBe(true);
});

test("reports the test excluded by only", () => {
  expect(true).toBe(true);
});
