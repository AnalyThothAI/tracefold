import { afterAll, expect, test } from "vitest";

test("the test body itself passes", () => {
  expect(true).toBe(true);
});

afterAll(() => {
  console.error("late afterAll console error");
});
