import { expect, test } from "@playwright/test";

test("selected plain pass", () => {
  expect(true).toBe(true);
});

test("excluded deliberate failure", () => {
  expect(true).toBe(false);
});
