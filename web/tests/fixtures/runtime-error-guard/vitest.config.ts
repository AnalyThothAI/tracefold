import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["tests/fixtures/runtime-error-guard/*.fixture.ts"],
    setupFiles: "./tests/setup.ts",
  },
});
