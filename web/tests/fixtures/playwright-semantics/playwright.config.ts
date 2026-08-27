import { defineConfig } from "@playwright/test";

const outputFile = process.env.TRACEFOLD_PLAYWRIGHT_SEMANTICS_REPORT;
if (!outputFile) throw new Error("TRACEFOLD_PLAYWRIGHT_SEMANTICS_REPORT is required");
const testMatch = process.env.TRACEFOLD_PLAYWRIGHT_FIXTURE ?? "expected-failure.spec.ts";

export default defineConfig({
  testDir: ".",
  testMatch,
  forbidOnly: true,
  fullyParallel: false,
  repeatEach: 1,
  retries: 0,
  workers: 1,
  reporter: [["json", { outputFile }], ["../../support/playwrightEvidenceReporter.ts"]],
});
