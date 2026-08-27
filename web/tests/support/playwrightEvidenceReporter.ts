import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";

import type { FullConfig, FullProject, Reporter, Suite } from "@playwright/test/reporter";

export const PLAYWRIGHT_SELECTION_SCHEMA_VERSION = "tracefold_playwright_selection_v1";

type SerializedPattern = { flags: string; source: string } | { literal: string };

export default class PlaywrightEvidenceReporter implements Reporter {
  onBegin(config: FullConfig, suite: Suite): void {
    const output = process.env.TRACEFOLD_PLAYWRIGHT_SELECTION_OUTPUT?.trim();
    if (!output) return;
    const root = process.cwd();
    const payload = {
      configFile: config.configFile ? relative(root, config.configFile) : "",
      forbidOnly: config.forbidOnly,
      fullyParallel: config.fullyParallel,
      grep: serializePatterns(config.grep),
      grepInvert: serializePatterns(config.grepInvert),
      invocation: process.argv.slice(2),
      maxFailures: config.maxFailures,
      projects: config.projects.map((project) => serializeProject(project, root)),
      schemaVersion: PLAYWRIGHT_SELECTION_SCHEMA_VERSION,
      selectedTestIds: suite.allTests().map((test) => test.id),
      selectedTestFiles: [
        ...new Set(suite.allTests().map((test) => relative(root, test.location.file))),
      ].sort(),
      shard: config.shard,
    };
    const path = resolve(output);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  }

  printsToStdio(): boolean {
    return false;
  }
}

function serializeProject(project: FullProject, root: string) {
  const use = project.use as FullProject["use"] & { defaultBrowserType?: string };
  return {
    browserName: use.browserName ?? use.defaultBrowserType,
    grep: serializePatterns(project.grep),
    grepInvert: serializePatterns(project.grepInvert),
    name: project.name,
    repeatEach: project.repeatEach,
    retries: project.retries,
    testDir: relative(root, project.testDir),
    testIgnore: serializePatterns(project.testIgnore),
    testMatch: serializePatterns(project.testMatch),
  };
}

function serializePatterns(
  value: null | string | RegExp | Array<string | RegExp>,
): SerializedPattern[] {
  if (value === null) return [];
  const values = Array.isArray(value) ? value : [value];
  return values.map((pattern) =>
    typeof pattern === "string"
      ? { literal: pattern }
      : { flags: pattern.flags, source: pattern.source },
  );
}
