import type { OpenApiStatusData } from "@lib/types";

const NOW = 1_777_770_000_000;

export function appStatusFixture(overrides: Partial<OpenApiStatusData> = {}): OpenApiStatusData {
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
      serve_runtime: {
        runtime_id: "1d36ca48-c41d-4d7b-a26d-86c2429a3e11",
        runtime_revision: "a521557",
        image_digest: "tracefold@sha256:" + "1".repeat(64),
        started_at_ms: NOW,
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
    ...overrides,
  };
}
