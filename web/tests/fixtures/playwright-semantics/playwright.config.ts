import { defineConfig } from "@playwright/test";

const outputFile = process.env.TRACEFOLD_PLAYWRIGHT_SEMANTICS_REPORT;
if (!outputFile) throw new Error("TRACEFOLD_PLAYWRIGHT_SEMANTICS_REPORT is required");

export default defineConfig({
  testDir: ".",
  testMatch: "expected-failure.spec.ts",
  forbidOnly: true,
  fullyParallel: false,
  repeatEach: 1,
  retries: 0,
  workers: 1,
  reporter: [["json", { outputFile }]],
});
