import { defineConfig, devices } from "@playwright/test";

const jsonOutput =
  process.env.PLAYWRIGHT_JSON_OUTPUT_NAME ?? "test-results/golden-path-results.json";

/*
 * Which spec runs at which viewport is decided here, at collection time, and nowhere else.
 *
 * It used to be decided at run time, by a `test.skip(!testInfo.project.name.startsWith("mobile-"))`
 * at the top of each spec. That is fine for a diagnostic lane and impossible for a required one:
 * `scripts/require_test_reports.py` rejects a Playwright report containing any skip, and the four
 * projects between them skipped 41 of 96 cases. The lists below are that same partition, moved to
 * where Playwright can honour it by never collecting the file — a filtered-out spec leaves no row
 * in the report, where a skipped one leaves a non-green outcome (#598 D8).
 *
 * A spec that reaches the wrong viewport now fails instead of quietly skipping, which is the
 * failure direction this lane wants.
 */
const specs = (...names: string[]) => names.map((name) => `**/${name}`);
const everyViewport = ["event-feed-controls.spec.ts", "price-plane.spec.ts"];
const desktop = specs(
  ...everyViewport,
  "news-event-drawer.spec.ts",
  "sidebar-navigation.spec.ts",
  "topbar-layout.spec.ts",
);
const tablet = specs(...everyViewport, "tablet-shell.spec.ts");
const mobile = specs(
  ...everyViewport,
  "mobile-navigation.spec.ts",
  "mobile-route-cold-load.spec.ts",
  "mobile-shell.spec.ts",
);

export default defineConfig({
  testDir: "./tests/e2e/golden-paths",
  failOnFlakyTests: true,
  forbidOnly: true,
  fullyParallel: false,
  repeatEach: 1,
  retries: 0,
  updateSnapshots: "none",
  reporter: [["list"], ["json", { outputFile: jsonOutput }]],
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:4173",
    colorScheme: "dark",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run build && npm run preview",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [
    {
      name: "desktop-1366",
      testMatch: desktop,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1366, height: 720 } },
    },
    {
      name: "desktop-1920",
      testMatch: desktop,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1920, height: 1080 } },
    },
    {
      name: "tablet-834",
      testMatch: tablet,
      use: {
        ...devices["iPad Pro 11"],
        browserName: "chromium",
        viewport: { width: 834, height: 1194 },
      },
    },
    {
      name: "mobile-390",
      testMatch: mobile,
      use: {
        ...devices["Pixel 5"],
        browserName: "chromium",
        viewport: { width: 390, height: 844 },
      },
    },
  ],
});
