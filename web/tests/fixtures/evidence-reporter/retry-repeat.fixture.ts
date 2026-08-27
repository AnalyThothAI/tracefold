import { expect, test } from "vitest";

let attempts = 0;

test("reports a test that passes on retry", { retry: 1 }, () => {
  attempts += 1;
  expect(attempts).toBe(2);
});

test("reports a repeated test", { repeats: 1 }, () => {
  expect(true).toBe(true);
});
