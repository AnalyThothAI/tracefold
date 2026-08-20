import type { Page, Route } from "@playwright/test";
import {
  newsEventDetailFixture,
  newsEventFixture,
  newsFeedEventFixture,
  newsFeedFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";

const NOW = 1_777_746_300_000;
const unhandledApiRequests = new WeakMap<Page, string[]>();

export type MockApiOptions = {
  delayNonBootstrapMs?: number;
  failNonBootstrap?: boolean;
};

export type MockFeedControl = {
  /** Put a newer Event at the top of the next poll, the way a live feed does. */
  prependEvent: (eventId: string) => void;
};

export async function installMockApi(
  page: Page,
  options: MockApiOptions = {},
): Promise<MockFeedControl> {
  unhandledApiRequests.set(page, []);
  const prepended: string[] = [];

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path !== "/api/bootstrap") {
      if (options.delayNonBootstrapMs) {
        await new Promise((resolve) => setTimeout(resolve, options.delayNonBootstrapMs));
      }
      if (options.failNonBootstrap) {
        return route.abort("failed");
      }
    }

    if (path === "/api/bootstrap") return fulfill(route, { ws_token: "secret" });
    if (path === "/api/status") return fulfill(route, statusData());
    if (path === "/api/news/feed") return fulfill(route, newsFeedData(prepended));
    if (path === "/api/news/status") return fulfill(route, newsStatusFixture());
    if (path.startsWith("/api/news/events/")) return fulfill(route, newsEventDetailData(path));
    recordUnhandledApiRequest(page, url);
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: `unhandled ${path}` }),
    });
  });

  return { prependEvent: (eventId: string) => prepended.push(eventId) };
}

export function getUnhandledApiRequests(page: Page): string[] {
  return [...(unhandledApiRequests.get(page) ?? [])];
}

function recordUnhandledApiRequest(page: Page, url: URL) {
  const requests = unhandledApiRequests.get(page) ?? [];
  requests.push(`${url.pathname}${url.search}`);
  unhandledApiRequests.set(page, requests);
}

function newsFeedData(prepended: string[] = []) {
  const event = newsFeedEventFixture({
    event_id: "evt-global-policy",
    leader_description:
      "Liquidity rotation is visible across crypto beta and rates-sensitive assets.",
    leader_title: "Macro desk flags liquidity rotation",
  });
  const row = (eventId: string, title: string) => ({
    ...event,
    title_zh: null,
    // No Chinese headline on the mock rows, so the wire line is the row headline (distinct per row).
    triage: event.triage ? { ...event.triage, headline_zh: null, title_zh: null } : null,
    event_id: eventId,
    leader_title: title,
  });
  return {
    ...newsFeedFixture(),
    events: [
      ...prepended.map((eventId) => row(eventId, `Breaking ${eventId}`)),
      ...Array.from({ length: 5 }, (_, index) =>
        index === 0
          ? row(event.event_id, event.leader_title)
          : row(`evt-global-policy-${index + 1}`, `Global policy update ${index + 1}`),
      ),
    ],
  };
}

function newsEventDetailData(path: string) {
  const eventId = decodeURIComponent(path.split("/").pop() ?? "evt-global-policy");
  return newsEventDetailFixture({
    event: newsEventFixture({
      event_id: eventId,
      leader_description:
        "Liquidity rotation is visible across crypto beta and rates-sensitive assets.",
      leader_title: "Macro desk flags liquidity rotation",
    }),
  });
}

async function fulfill(route: Route, data: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ ok: true, data }),
  });
}

function statusData() {
  return {
    measured_at_ms: NOW,
    runtime: {
      ok: true,
      reasons: [],
      db: {
        ok: true,
        schema_ok: true,
        current_revision: "20260812_0255",
        expected_revision: "20260812_0255",
        error_code: null,
      },
      workers_runtime: {
        runtime_id: "1d36ca48-c41d-4d7b-a26d-86c2429a3e10",
        runtime_version: "a521557",
        state: "running",
        started_at_ms: NOW,
        heartbeat_at_ms: NOW,
        heartbeat_stale_after_ms: 15_000,
        fatal_code: null,
        unavailable_reason: null,
      },
    },
  };
}
