import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.TRACEFOLD_FULL_STACK_URL;
if (!baseURL) {
  throw new Error("TRACEFOLD_FULL_STACK_URL is required for the real full-stack lane.");
}
const jsonOutput =
  process.env.PLAYWRIGHT_JSON_OUTPUT_NAME ?? "test-results/full-stack-results.json";

export default defineConfig({
  testDir: "./tests/e2e/full-stack",
  forbidOnly: true,
  fullyParallel: false,
  repeatEach: 1,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: jsonOutput }]],
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    colorScheme: "dark",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "required-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1366, height: 720 } },
    },
  ],
});
