import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/fixtures/evidence-reporter/*.fixture.ts"],
  },
});
