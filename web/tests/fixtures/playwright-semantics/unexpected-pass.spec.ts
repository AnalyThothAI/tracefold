import { expect, test } from "@playwright/test";

test("fixture proving that an expected failure can unexpectedly pass", () => {
  test.fail(true, "evidence must report an unexpected pass separately");
  expect(true).toBe(true);
});
