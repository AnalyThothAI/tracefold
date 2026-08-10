import { readFileSync, writeFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

const readyPath = requiredEnvironment("TRACEFOLD_RADAR_BROWSER_READY_PATH");
const evidencePath = requiredEnvironment("TRACEFOLD_RADAR_EVIDENCE_PATH");
const timingsPath = requiredEnvironment("TRACEFOLD_RADAR_TIMINGS_PATH");

type RadarFetchTiming = {
  id: number;
  method: string;
  startTime: number;
  headersTime: number | null;
  endTime: number | null;
  status: number | null;
  ifNoneMatch: string | null;
  responseEtag: string | null;
};

type RadarLongTaskTiming = {
  startTime: number;
  duration: number;
  endTime: number;
};

type RadarLongAnimationFrameTiming = Record<string, unknown> & {
  startTime: number;
  duration: number;
};

type RadarRenderTiming = {
  requestId: number;
  mutationTime: number;
  nextFrameTime: number | null;
  visibleTime: number | null;
  visibility: {
    isIntersecting: boolean;
    intersectionWidth: number;
    intersectionHeight: number;
    boundingWidth: number;
    boundingHeight: number;
    visible: boolean;
  } | null;
};

type RadarTimingState = {
  timeOrigin: number;
  fetches: RadarFetchTiming[];
  longTasks: RadarLongTaskTiming[];
  longAnimationFrames: RadarLongAnimationFrameTiming[];
  render: RadarRenderTiming | null;
};

type RadarWindow = typeof window & {
  __tokenRadarTimings?: RadarTimingState;
};

test.use({ trace: "off" });

test("renders a real Token Radar publication within one polling interval", async ({
  page,
}, testInfo) => {
  test.setTimeout(70_000);
  await page.addInitScript(() => {
    const target = window as RadarWindow;
    const timings: RadarTimingState = {
      timeOrigin: performance.timeOrigin,
      fetches: [],
      longTasks: [],
      longAnimationFrames: [],
      render: null,
    };
    target.__tokenRadarTimings = timings;
    const originalFetch = window.fetch.bind(window);
    let nextRequestId = 1;
    window.fetch = async (...args) => {
      const input = args[0];
      const rawUrl =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const isTokenRadar = new URL(rawUrl, window.location.href).pathname === "/api/token-radar";
      if (!isTokenRadar) return originalFetch(...args);
      const init = args[1];
      const headers = new Headers(
        init?.headers ?? (input instanceof Request ? input.headers : undefined),
      );

      const timing: RadarFetchTiming = {
        id: nextRequestId++,
        method: (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase(),
        startTime: performance.now(),
        headersTime: null,
        endTime: null,
        status: null,
        ifNoneMatch: headers.get("if-none-match"),
        responseEtag: null,
      };
      timings.fetches.push(timing);
      const response = await originalFetch(...args);
      timing.headersTime = performance.now();
      timing.status = response.status;
      timing.responseEtag = response.headers.get("etag");
      const finish = () => {
        timing.endTime ??= performance.now();
      };
      if (response.status === 304) {
        finish();
        return response;
      }
      const originalJson = response.json.bind(response);
      const originalText = response.text.bind(response);
      response.json = async () => {
        try {
          return await originalJson();
        } finally {
          finish();
        }
      };
      response.text = async () => {
        try {
          return await originalText();
        } finally {
          finish();
        }
      };
      return response;
    };

    new PerformanceObserver((entries) => {
      timings.longTasks.push(
        ...entries.getEntries().map((entry) => ({
          startTime: entry.startTime,
          duration: entry.duration,
          endTime: entry.startTime + entry.duration,
        })),
      );
    }).observe({ type: "longtask", buffered: true });

    if (PerformanceObserver.supportedEntryTypes.includes("long-animation-frame")) {
      new PerformanceObserver((entries) => {
        timings.longAnimationFrames.push(
          ...entries.getEntries().map((entry) => entry.toJSON() as RadarLongAnimationFrameTiming),
        );
      }).observe({ type: "long-animation-frame", buffered: true });
    }
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Radar" })).toBeVisible();
  await expect(page.getByText("No eligible cases")).toBeVisible();
  await page.evaluate(() => {
    const timings = (window as RadarWindow).__tokenRadarTimings;
    const radarQueue = document.querySelector(".live-radar-queue");
    if (!timings || !radarQueue) {
      throw new Error("Token Radar timing state or queue is unavailable.");
    }
    new MutationObserver((records) => {
      if (timings.render) return;
      const mutationTime = performance.now();
      if (records.length === 0) return;
      const targetItem = [...radarQueue.querySelectorAll(".live-radar-item")].find((item) =>
        item.textContent?.includes("$E2ERADAR"),
      );
      if (!targetItem) return;
      const request = [...timings.fetches]
        .reverse()
        .find((candidate) => candidate.endTime !== null && candidate.endTime <= mutationTime);
      if (!request) return;
      const render: RadarRenderTiming = {
        requestId: request.id,
        mutationTime,
        nextFrameTime: null,
        visibleTime: null,
        visibility: null,
      };
      timings.render = render;
      const visibilityObserver = new IntersectionObserver((entries) => {
        const entry = entries.find((candidate) => candidate.target === targetItem);
        if (!entry) return;
        const visible =
          entry.isIntersecting &&
          entry.intersectionRect.width > 0 &&
          entry.intersectionRect.height > 0;
        render.visibility = {
          isIntersecting: entry.isIntersecting,
          intersectionWidth: entry.intersectionRect.width,
          intersectionHeight: entry.intersectionRect.height,
          boundingWidth: entry.boundingClientRect.width,
          boundingHeight: entry.boundingClientRect.height,
          visible,
        };
        if (!visible) return;
        render.nextFrameTime = entry.time;
        render.visibleTime = performance.now();
        visibilityObserver.disconnect();
      });
      visibilityObserver.observe(targetItem);
    }).observe(radarQueue, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });
  });
  writeFileSync(readyPath, "browser-ready\n", "utf8");

  // Leave the measured browser interval free of Playwright polling/evaluation tasks.
  await new Promise((resolve) => setTimeout(resolve, 35_000));
  const evidence = JSON.parse(readFileSync(evidencePath, "utf8")) as { persisted_at_ms: number };
  const timings = await page.evaluate(() => (window as RadarWindow).__tokenRadarTimings ?? null);
  expect(timings).not.toBeNull();
  if (
    !timings?.render ||
    timings.render.nextFrameTime === null ||
    timings.render.visibleTime === null ||
    !timings.render.visibility?.visible
  ) {
    throw new Error("Token Radar target did not become visibly rendered in the measured frame.");
  }
  const request = timings.fetches.find((candidate) => candidate.id === timings.render?.requestId);
  expect(request).toBeDefined();
  if (!request || request.headersTime === null || request.endTime === null) {
    throw new Error("Token Radar update request timing is incomplete.");
  }
  expect(request.status).toBe(200);
  expect(request.method).toBe("GET");
  expect(request.startTime).toBeLessThanOrEqual(request.endTime);
  expect(request.endTime).toBeLessThanOrEqual(timings.render.mutationTime);
  expect(timings.render.mutationTime).toBeLessThanOrEqual(timings.render.nextFrameTime);
  expect(timings.render.nextFrameTime).toBeLessThanOrEqual(timings.render.visibleTime);

  const initialRequest = timings.fetches[0];
  expect(initialRequest).toBeDefined();
  if (!initialRequest || initialRequest.endTime === null) {
    throw new Error("Initial Token Radar request timing is incomplete.");
  }
  expect(initialRequest.status).toBe(200);
  expect(initialRequest.method).toBe("GET");
  expect(initialRequest.ifNoneMatch).toBeNull();
  expect(initialRequest.responseEtag).not.toBeNull();
  expect(request.id).not.toBe(initialRequest.id);
  expect(request.ifNoneMatch).toBe(initialRequest.responseEtag);
  const pollStartDeltaMs = request.startTime - initialRequest.startTime;
  expect(pollStartDeltaMs).toBeGreaterThanOrEqual(28_000);
  expect(pollStartDeltaMs).toBeLessThanOrEqual(35_000);

  const visibleAtMs = Math.round(timings.timeOrigin + timings.render.visibleTime);
  const persistedToVisibleMs = visibleAtMs - evidence.persisted_at_ms;
  expect(persistedToVisibleMs).toBeGreaterThanOrEqual(0);
  expect(persistedToVisibleMs).toBeLessThanOrEqual(60_000);

  const intervalLongTasks = timings.longTasks.filter(
    (task) => task.startTime < timings.render!.visibleTime! && task.endTime > request.headersTime!,
  );
  const domNodeCount = await page.locator("*").count();
  const audit = {
    persisted_at_ms: evidence.persisted_at_ms,
    visible_at_ms: visibleAtMs,
    persisted_to_visible_ms: persistedToVisibleMs,
    poll_start_delta_ms: pollStartDeltaMs,
    initial_request: initialRequest,
    update_request: request,
    target_render: timings.render,
    update_window: {
      start: "response_headers",
      start_time: request.headersTime,
      end: "target_visible",
      end_time: timings.render.visibleTime,
    },
    request_to_next_frame_ms: timings.render.nextFrameTime - request.startTime,
    headers_to_next_frame_ms: timings.render.nextFrameTime - request.headersTime,
    body_end_to_next_frame_ms: timings.render.nextFrameTime - request.endTime,
    request_to_visible_ms: timings.render.visibleTime - request.startTime,
    headers_to_visible_ms: timings.render.visibleTime - request.headersTime,
    body_end_to_visible_ms: timings.render.visibleTime - request.endTime,
    interval_long_tasks: intervalLongTasks,
    dom_node_count: domNodeCount,
    all_token_radar_fetches: timings.fetches,
    all_long_tasks: timings.longTasks,
    all_long_animation_frames: timings.longAnimationFrames,
  };
  writeFileSync(timingsPath, JSON.stringify(audit, null, 2), "utf8");
  await testInfo.attach("token-radar-browser-timings", {
    body: Buffer.from(JSON.stringify(audit, null, 2)),
    contentType: "application/json",
  });
  console.log(`TOKEN_RADAR_BROWSER_TIMINGS ${JSON.stringify(audit)}`);
  expect(intervalLongTasks.filter((task) => task.duration > 50)).toEqual([]);

  const item = page.locator(".live-radar-item").filter({ hasText: "$E2ERADAR" });
  await expect(item).toHaveCount(1);
  await expect(item).toBeVisible();
  await expect(item.getByRole("img", { name: "E2E Radar icon" })).toBeVisible();
  await expect(item.getByRole("group", { name: "Price $12.00" })).toBeVisible();
  await expect(item.getByRole("group", { name: "Since signal +20%" })).toBeVisible();
  await expect(item.getByRole("group", { name: "Market cap $12M" })).toBeVisible();
  expect(domNodeCount).toBeLessThanOrEqual(1_100);
});

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required for Token Radar release evidence.`);
  return value;
}
