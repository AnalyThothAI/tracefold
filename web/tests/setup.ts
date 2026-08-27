import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { toHaveNoViolations } from "jest-axe";
import { afterAll, afterEach, beforeAll, beforeEach, expect } from "vitest";

import { server } from "./msw/server";
import {
  beginRuntimeErrorGuard,
  finishRuntimeErrorGuard,
  installRuntimeErrorGuard,
  uninstallRuntimeErrorGuard,
} from "./support/runtimeErrorGuard";

expect.extend(toHaveNoViolations);

configure({ asyncUtilTimeout: 5_000 });

/**
 * jsdom has no layout, so `matchMedia` has to be told what width it is standing at. The shell now *mounts*
 * a different navigation per breakpoint rather than hiding one with CSS, so a stub that answered `false` to
 * everything left component tests on a tablet with a closed drawer and no navigation at all.
 *
 * The nominal width is desktop, which is the console's primary surface. Phone and tablet frames are a
 * layout question and are covered where layout exists — the Playwright `mobile-390` and `tablet-834`
 * projects.
 */
const NOMINAL_TEST_WIDTH = 1440;
let unhandledRequests: string[] = [];

installRuntimeErrorGuard();

server.events.on("request:unhandled", ({ request }) => {
  const url = new URL(request.url);
  unhandledRequests.push(`${request.method} ${url.pathname}`);
});

function matchesWidthQuery(query: string): boolean {
  const min = /min-width:\s*(\d+(?:\.\d+)?)px/.exec(query);
  const max = /max-width:\s*(\d+(?:\.\d+)?)px/.exec(query);
  if (min && NOMINAL_TEST_WIDTH < Number(min[1])) return false;
  if (max && NOMINAL_TEST_WIDTH > Number(max[1])) return false;
  return Boolean(min || max);
}

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      addEventListener: () => undefined,
      addListener: () => undefined,
      dispatchEvent: () => false,
      matches: matchesWidthQuery(query),
      media: query,
      onchange: null,
      removeEventListener: () => undefined,
      removeListener: () => undefined,
    }),
  });
  server.listen({ onUnhandledRequest: "error" });
});
beforeEach(() => {
  unhandledRequests = [];
  beginRuntimeErrorGuard();
});
afterEach(async () => {
  // Unmount before removing runtime handlers so query cancellation cannot race a handler reset and turn
  // an otherwise covered poll into a request:unhandled event between tests.
  cleanup();
  server.resetHandlers();
  const failures = await finishRuntimeErrorGuard();
  if (unhandledRequests.length > 0) {
    failures.push(
      `Unhandled API requests must have explicit MSW handlers:\n${unhandledRequests.join("\n")}`,
    );
  }
  if (failures.length > 0) throw new Error(failures.join("\n\n"));
});
afterAll(() => {
  uninstallRuntimeErrorGuard();
  server.close();
});
