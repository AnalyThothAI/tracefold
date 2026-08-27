import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import type {
  Reporter,
  SerializedError,
  TestCase,
  TestModule,
  TestRunEndReason,
  Vitest,
} from "vitest/node";

export const EVIDENCE_REPORT_SCHEMA_VERSION = "tracefold_vitest_report_v3";

interface EvidenceReporterOptions {
  outputFile?: string;
}

interface EvidenceError {
  message: string;
  name?: string;
  stack?: string;
}

interface EvidenceTestResult {
  errors: EvidenceError[];
  fails: boolean;
  file: string;
  finalState: "failed" | "passed" | "pending" | "skipped";
  flaky: boolean;
  id: string;
  mode: "only" | "run" | "skip" | "todo";
  name: string;
  only: boolean;
  repeatCount: number;
  repeats: number;
  retry: number | { condition?: string; count: number; delay: number };
  retryCount: number;
  state: "failed" | "passed" | "pending" | "skipped";
}

export interface EvidenceReport {
  allowOnly: boolean;
  invocation: string[];
  moduleErrors: EvidenceError[];
  numExpectedFailures: number;
  numFailedTests: number;
  numFlakyTests: number;
  numOnlyTests: number;
  numPassedTests: number;
  numPendingTests: number;
  numRepeatedTests: number;
  numRetriedTests: number;
  numTodoTests: number;
  numTotalTests: number;
  numXfailedTests: number;
  numXpassedTests: number;
  reason: TestRunEndReason;
  schemaVersion: typeof EVIDENCE_REPORT_SCHEMA_VERSION;
  success: boolean;
  testFiles: string[];
  tests: EvidenceTestResult[];
  unhandledErrors: EvidenceError[];
}

export default class EvidenceReporter implements Reporter {
  private context: Vitest | undefined;

  constructor(private readonly options: EvidenceReporterOptions = {}) {}

  onInit(context: Vitest): void {
    this.context = context;
  }

  async onTestRunEnd(
    testModules: ReadonlyArray<TestModule>,
    unhandledErrors: ReadonlyArray<SerializedError>,
    reason: TestRunEndReason,
  ): Promise<void> {
    const tests = testModules.flatMap((testModule) =>
      [...testModule.children.allTests()].map(toEvidenceTest),
    );
    const moduleErrors = testModules.flatMap((testModule) =>
      testModule.errors().map(toEvidenceError),
    );
    const testFiles = [
      ...new Set(testModules.map((testModule) => testModule.relativeModuleId)),
    ].sort();
    const serializedUnhandledErrors = unhandledErrors.map(toEvidenceError);
    const numExpectedFailures = tests.filter((test) => test.fails).length;
    const numFailedTests = tests.filter(
      (test) => !test.fails && test.finalState === "failed",
    ).length;
    const numFlakyTests = tests.filter((test) => test.flaky).length;
    const numOnlyTests = tests.filter((test) => test.only).length;
    const numPassedTests = tests.filter(
      (test) => !test.fails && test.finalState === "passed",
    ).length;
    const numPendingTests = tests.filter(
      (test) =>
        test.finalState === "pending" || (test.finalState === "skipped" && test.mode !== "todo"),
    ).length;
    const numRepeatedTests = tests.filter(
      (test) => test.repeats > 0 || test.repeatCount > 0,
    ).length;
    const numRetriedTests = tests.filter(
      (test) => retryCount(test.retry) > 0 || test.retryCount > 0,
    ).length;
    const numTodoTests = tests.filter(
      (test) => test.finalState === "skipped" && test.mode === "todo",
    ).length;
    const numXfailedTests = tests.filter(
      (test) => test.fails && test.finalState === "passed",
    ).length;
    const numXpassedTests = tests.filter(
      (test) => test.fails && test.finalState === "failed",
    ).length;
    const allowOnly = this.context?.config.allowOnly ?? true;
    const report: EvidenceReport = {
      allowOnly,
      invocation: process.argv.slice(2),
      moduleErrors,
      numExpectedFailures,
      numFailedTests,
      numFlakyTests,
      numOnlyTests,
      numPassedTests,
      numPendingTests,
      numRepeatedTests,
      numRetriedTests,
      numTodoTests,
      numTotalTests: tests.length,
      numXfailedTests,
      numXpassedTests,
      reason,
      schemaVersion: EVIDENCE_REPORT_SCHEMA_VERSION,
      success:
        reason === "passed" &&
        tests.length > 0 &&
        numPassedTests === tests.length &&
        !allowOnly &&
        numExpectedFailures === 0 &&
        numFlakyTests === 0 &&
        numOnlyTests === 0 &&
        numRepeatedTests === 0 &&
        numRetriedTests === 0 &&
        moduleErrors.length === 0 &&
        serializedUnhandledErrors.length === 0,
      testFiles,
      tests,
      unhandledErrors: serializedUnhandledErrors,
    };

    await this.writeReport(report);
  }

  private async writeReport(report: EvidenceReport): Promise<void> {
    const output = `${JSON.stringify(report, null, 2)}\n`;
    const configuredOutput =
      process.env.TRACEFOLD_VITEST_SEMANTICS_REPORT?.trim() ||
      this.options.outputFile ||
      resolveConfiguredOutput(this.context);
    if (!configuredOutput) {
      this.context?.logger.log(output.trimEnd());
      return;
    }

    const reportFile = resolve(this.context?.config.root ?? process.cwd(), configuredOutput);
    await mkdir(dirname(reportFile), { recursive: true });
    await writeFile(reportFile, output, "utf8");
  }
}

function toEvidenceTest(test: TestCase): EvidenceTestResult {
  const result = test.result();
  const diagnostic = test.diagnostic();
  const errors = result.errors?.map(toEvidenceError) ?? [];
  return {
    errors,
    fails: test.options.fails === true,
    file: test.module.relativeModuleId,
    finalState: result.state,
    flaky: diagnostic?.flaky ?? false,
    id: test.id,
    mode: test.options.mode,
    name: test.fullName,
    only: test.options.mode === "only" || errors.some(isOnlyViolation),
    repeatCount: diagnostic?.repeatCount ?? 0,
    repeats: test.options.repeats ?? 0,
    retry: serializeRetry(test.options.retry),
    retryCount: diagnostic?.retryCount ?? 0,
    state: result.state,
  };
}

function serializeRetry(retry: TestCase["options"]["retry"]): EvidenceTestResult["retry"] {
  if (typeof retry === "number" || retry === undefined) return retry ?? 0;
  return {
    condition: retry.condition ? `/${retry.condition.source}/${retry.condition.flags}` : undefined,
    count: retry.count ?? 0,
    delay: retry.delay ?? 0,
  };
}

function retryCount(retry: EvidenceTestResult["retry"]): number {
  return typeof retry === "number" ? retry : retry.count;
}

function isOnlyViolation(error: EvidenceError): boolean {
  return error.message.startsWith("[Vitest] Unexpected .only modifier.");
}

function toEvidenceError(error: SerializedError): EvidenceError {
  return {
    message: error.message ?? String(error),
    ...(error.name ? { name: error.name } : {}),
    ...(error.stack ? { stack: error.stack } : {}),
  };
}

function resolveConfiguredOutput(context: Vitest | undefined): string | undefined {
  const configured = context?.config.outputFile;
  if (typeof configured === "string") return configured;
  if (!configured) return undefined;

  const outputs = [...new Set(Object.values(configured))];
  return outputs.length === 1 ? outputs[0] : undefined;
}
