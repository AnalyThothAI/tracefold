import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const fixtureConfig = resolve("tests/fixtures/runtime-error-guard/vitest.config.ts");
const vitestEntrypoint = resolve("node_modules/vitest/vitest.mjs");
const fixtureResults = new Map<string, ReturnType<typeof executeFixture>>();

function executeFixture(name: string) {
  const fixture = resolve(`tests/fixtures/runtime-error-guard/${name}.fixture.ts`);
  const result = spawnSync(
    process.execPath,
    [vitestEntrypoint, "run", "--config", fixtureConfig, "--no-color", fixture],
    {
      cwd: process.cwd(),
      encoding: "utf8",
      env: { ...process.env, NO_COLOR: "1" },
    },
  );

  if (result.error) throw result.error;
  return {
    output: `${result.stdout}\n${result.stderr}`,
    status: result.status,
  };
}

function runFixture(name: string) {
  const cached = fixtureResults.get(name);
  if (cached) return cached;
  const result = executeFixture(name);
  fixtureResults.set(name, result);
  return result;
}

describe("Vitest runtime error guard", () => {
  it("fails the case in afterEach when console.error is unexpected", () => {
    const result = runFixture("fail-closed");

    expect(result.status, result.output).not.toBe(0);
    expect(result.output).toContain("Unexpected console.error in test case");
  });

  it("fails the case in afterEach when a rejection is unhandled", () => {
    const result = runFixture("fail-closed");

    expect(result.status, result.output).not.toBe(0);
    expect(result.output).toContain("Unexpected unhandled rejection in test case");
  });

  it("permits matching runtime errors with case-local reasons", () => {
    const result = runFixture("allowed-errors");

    expect(result.status, result.output).toBe(0);
  });

  it("rejects file-global allowlists", () => {
    const result = runFixture("global-allowlist");

    expect(result.status, result.output).not.toBe(0);
    expect(result.output).toContain(
      "Runtime error allowlists are case-local and must be set inside a test",
    );
  });

  it("rejects allowlists without a reason", () => {
    const result = runFixture("fail-closed");

    expect(result.status, result.output).not.toBe(0);
    expect(result.output).toContain("Runtime error allowlists require a non-empty reason");
  });
});
