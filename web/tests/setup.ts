import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { toHaveNoViolations } from "jest-axe";
import { afterAll, afterEach, beforeAll, expect } from "vitest";

import { server } from "./msw/server";

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
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
