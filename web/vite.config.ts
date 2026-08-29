import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

const srcPath = (path: string) => new URL(`./src/${path}`, import.meta.url).pathname;
const testsPath = (path: string) => new URL(`./tests/${path}`, import.meta.url).pathname;
const devApiProxyTarget = process.env.VITE_DEV_API_PROXY_TARGET ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@app": srcPath("app"),
      "@routes": srcPath("routes"),
      "@features": srcPath("features"),
      "@shared": srcPath("shared"),
      "@lib": srcPath("lib"),
      "@tests": testsPath(""),
    },
  },
  server: {
    proxy: {
      "/api": devApiProxyTarget,
    },
  },
  test: {
    allowOnly: false,
    /*
     * Report-only coverage (#373). Nothing here turns coverage on: it activates only for the run
     * that passes `--coverage`, which is the single unit/component/routes pass `make ci-frontend`
     * already executes. `test:architecture` and Playwright therefore never re-run for a percentage.
     * Native V8 output only — no custom reporter, and no threshold until PR 3 measures one.
     */
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/lib/types/**"],
      reporter: ["text-summary", "json", "lcov", "html"],
      reportsDirectory: "../artifacts/coverage/frontend",
    },
    environment: "jsdom",
    exclude: [...configDefaults.exclude, "tests/e2e/**"],
    passWithNoTests: false,
    retry: 0,
    setupFiles: "./tests/setup.ts",
    /*
     * Strictly above the 5 s `asyncUtilTimeout` in `tests/setup.ts`. Equal to it, a `findBy*` that is about
     * to fail is killed by the test timeout first, so the failure reports "Test timed out" with no DOM
     * dump instead of Testing Library's "Unable to find…" — and a route test doing several sequential
     * `findBy` calls fails on a loaded suite while passing alone, which reads as flake rather than load.
     * A real missing element still fails in ~5 s; only the queueing has room now.
     */
    testTimeout: 15_000,
  },
});
