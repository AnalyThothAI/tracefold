import { expect, test } from "@playwright/test";

test("fixture proving that test.fail is not a plain pass", () => {
  test.fail(true, "evidence must reject expected failures in required lanes");
  expect(1).toBe(2);
});
