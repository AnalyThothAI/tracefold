import {
  expect,
  test as base,
  type ConsoleMessage,
  type Page,
  type Request,
} from "@playwright/test";
import { getUnhandledApiRequests } from "@tests/e2e/support/mockApi";

type GuardFailure = {
  detail: string;
  kind: "console.error" | "pageerror" | "requestfailed" | "unhandled-api";
};

type BrowserFailureAllowance = {
  kind: GuardFailure["kind"];
  match: RegExp | string;
  reason: string;
};

const allowances = new WeakMap<Page, BrowserFailureAllowance[]>();

/** A single case may name one expected browser failure and why that failure is the behavior under test. */
export function allowBrowserFailure(page: Page, allowance: BrowserFailureAllowance): void {
  if (allowance.reason.trim() === "") {
    throw new Error("An expected browser failure requires a case-local reason.");
  }
  allowances.set(page, [...(allowances.get(page) ?? []), allowance]);
}

/**
 * Every browser case fails closed on failures outside its assertions. HTTP error responses remain ordinary
 * application inputs; browser/runtime failures and mock routes that nobody owns are test failures.
 */
export const test = base.extend<{ browserFailureGuard: void }>({
  browserFailureGuard: [
    async ({ page }, use) => {
      const failures: GuardFailure[] = [];
      allowances.set(page, []);
      const onConsole = (message: ConsoleMessage) => {
        if (message.type() === "error") {
          failures.push({ kind: "console.error", detail: scrub(message.text()) });
        }
      };
      const onPageError = (error: Error) => {
        failures.push({ kind: "pageerror", detail: scrub(error.message) });
      };
      const onRequestFailed = (request: Request) => {
        failures.push({
          kind: "requestfailed",
          detail: `${request.method()} ${safePath(request.url())} (${scrub(
            request.failure()?.errorText ?? "unknown browser failure",
          )})`,
        });
      };

      page.on("console", onConsole);
      page.on("pageerror", onPageError);
      page.on("requestfailed", onRequestFailed);
      await use();

      for (const request of getUnhandledApiRequests(page)) {
        failures.push({ kind: "unhandled-api", detail: request });
      }
      page.off("console", onConsole);
      page.off("pageerror", onPageError);
      page.off("requestfailed", onRequestFailed);

      const unexpected = failures.filter((failure) =>
        (allowances.get(page) ?? []).every((allowance) => !matches(allowance, failure)),
      );
      expect(
        unexpected,
        `Unexpected browser failures:\n${unexpected
          .map(({ detail, kind }) => `- ${kind}: ${detail}`)
          .join("\n")}`,
      ).toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
export type { Locator, Page } from "@playwright/test";

function safePath(rawUrl: string): string {
  try {
    return new URL(rawUrl).pathname;
  } catch {
    return "<invalid-url>";
  }
}

function scrub(message: string): string {
  return message
    .replace(/Bearer\s+[^\s"']+/gi, "Bearer <redacted>")
    .replace(/([?&](?:token|ws_token)=)[^&\s]+/gi, "$1<redacted>")
    .slice(0, 1_000);
}

function matches(allowance: BrowserFailureAllowance, failure: GuardFailure): boolean {
  if (allowance.kind !== failure.kind) return false;
  if (typeof allowance.match === "string") return allowance.match === failure.detail;
  allowance.match.lastIndex = 0;
  return allowance.match.test(failure.detail);
}
