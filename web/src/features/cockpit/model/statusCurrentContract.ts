import type { OpenApiStatusData, WorkerStatusData } from "@lib/types";

type JsonRecord = Record<string, unknown>;

const STATUS_KEYS = [
  "ok",
  "reasons",
  "handles",
  "store",
  "snapshot_gate",
  "db",
  "news",
  "provider_states",
  "workers",
] as const;
const NEWS_KEYS = ["status", "reasons", "layers", "measured_at_ms"] as const;
const NEWS_LAYER_NAMES = ["ingest", "story", "brief"] as const;
const WORKER_KEYS = [
  "enabled",
  "running",
  "effective_status",
  "unavailable_reason",
  "last_started_at_ms",
  "last_finished_at_ms",
  "last_result",
  "last_error",
  "iteration_duration_p99_ms",
] as const;
export function requireStatusData(value: unknown): OpenApiStatusData {
  const status = requireRecord(value, "status");
  requireExactKeys(status, STATUS_KEYS, "status");
  requireBoolean(status.ok, "status.ok");
  requireStringArray(status.reasons, "status.reasons");
  requireStringArray(status.handles, "status.handles");
  requireString(status.store, "status.store");
  requireRecord(status.snapshot_gate, "status.snapshot_gate");
  requireRecord(status.db, "status.db");
  requireNewsHealth(status.news);
  requireRecord(status.provider_states, "status.provider_states");
  const workers = requireRecord(status.workers, "status.workers");
  if (!Object.hasOwn(workers, "collector")) {
    fail("status.workers.collector");
  }
  for (const [workerName, workerValue] of Object.entries(workers)) {
    requireWorkerStatusData(workerValue, `status.workers.${workerName}`);
  }
  return status as OpenApiStatusData;
}

function requireNewsHealth(value: unknown): void {
  const news = requireRecord(value, "status.news");
  requireExactKeys(news, NEWS_KEYS, "status.news");
  requireHealthStatus(news.status, "status.news.status");
  requireFiniteNumber(news.measured_at_ms, "status.news.measured_at_ms");
  requireStringArray(news.reasons, "status.news.reasons");
  const layers = requireRecord(news.layers, "status.news.layers");
  requireExactKeys(layers, NEWS_LAYER_NAMES, "status.news.layers");
  for (const layerName of NEWS_LAYER_NAMES) {
    const path = `status.news.layers.${layerName}`;
    const layer = requireRecord(layers[layerName], path);
    requireHealthStatus(layer.status, `${path}.status`);
  }
}

function requireHealthStatus(value: unknown, path: string): void {
  if (value !== "healthy" && value !== "degraded" && value !== "unavailable") fail(path);
}

export function requireWorkerStatusData(value: unknown, path = "worker"): WorkerStatusData {
  const worker = requireRecord(value, path);
  requireExactKeys(worker, WORKER_KEYS, path);
  requireBoolean(worker.enabled, `${path}.enabled`);
  requireBoolean(worker.running, `${path}.running`);
  requireString(worker.effective_status, `${path}.effective_status`);
  requireNullableString(worker.unavailable_reason, `${path}.unavailable_reason`);
  requireNullableFiniteNumber(worker.last_started_at_ms, `${path}.last_started_at_ms`);
  requireNullableFiniteNumber(worker.last_finished_at_ms, `${path}.last_finished_at_ms`);
  requireNullableRecord(worker.last_result, `${path}.last_result`);
  requireNullableString(worker.last_error, `${path}.last_error`);
  requireNullableFiniteNumber(
    worker.iteration_duration_p99_ms,
    `${path}.iteration_duration_p99_ms`,
  );
  return worker as WorkerStatusData;
}

function requireExactKeys(value: JsonRecord, keys: readonly string[], path: string): void {
  const actual = Object.keys(value);
  const unknown = actual.find((key) => !keys.includes(key));
  if (unknown) fail(`${path}.${unknown}`);
  const missing = keys.find((key) => !Object.hasOwn(value, key));
  if (missing) fail(`${path}.${missing}`);
}

function requireRecord(value: unknown, path: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(path);
  return value as JsonRecord;
}

function requireNullableRecord(value: unknown, path: string): JsonRecord | null {
  return value === null ? null : requireRecord(value, path);
}

function requireStringArray(value: unknown, path: string): string[] {
  if (!Array.isArray(value)) fail(path);
  return value.map((item, index) => requireString(item, `${path}.${index}`));
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== "string") fail(path);
  return value;
}

function requireNullableString(value: unknown, path: string): string | null {
  if (value !== null && typeof value !== "string") fail(path);
  return value;
}

function requireNullableFiniteNumber(value: unknown, path: string): number | null {
  if (value !== null && (typeof value !== "number" || !Number.isFinite(value))) fail(path);
  return value;
}

function requireFiniteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(path);
  return value;
}

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") fail(path);
  return value;
}

function fail(path: string): never {
  throw new Error(`status_current_contract:${path}`);
}
