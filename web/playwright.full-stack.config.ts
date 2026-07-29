import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.TRACEFOLD_FULL_STACK_URL;
if (!baseURL) {
  throw new Error("TRACEFOLD_FULL_STACK_URL is required for the real full-stack lane.");
}

export default defineConfig({
  testDir: "./tests/e2e/full-stack",
  fullyParallel: false,
  reporter: "list",
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
      name: "desktop-1920",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1920, height: 1080 } },
    },
    {
      name: "desktop-1366",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1366, height: 720 } },
    },
    {
      name: "tablet-834",
      use: {
        ...devices["iPad Pro 11"],
        browserName: "chromium",
        viewport: { width: 834, height: 1194 },
      },
    },
    {
      name: "mobile-390",
      use: {
        ...devices["Pixel 5"],
        browserName: "chromium",
        viewport: { width: 390, height: 844 },
      },
    },
  ],
});
