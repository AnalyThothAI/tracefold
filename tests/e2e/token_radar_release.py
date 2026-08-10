"""Test-only orchestration for real Token Radar browser release evidence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import psycopg

_ROOT = Path(__file__).resolve().parents[2]
_WEB_ROOT = _ROOT / "web"
_BROWSER_READY_TIMEOUT_SECONDS = 20.0
_PLAYWRIGHT_TIMEOUT_SECONDS = 75.0
_WORKER_TIMEOUT_SECONDS = 15.0


def run_token_radar_browser_release(
    *,
    postgres_dsn: str,
    base_url: str,
    coordination_dir: Path,
) -> None:
    """Prove one persisted fact reaches the real served browser without mocks."""
    ready_path = coordination_dir / "browser-ready"
    evidence_path = coordination_dir / "radar-evidence.json"
    timings_path = coordination_dir / "radar-browser-timings.json"
    process = subprocess.Popen(
        [
            "npm",
            "exec",
            "--",
            "playwright",
            "test",
            "--config",
            "playwright.full-stack.config.ts",
            "tests/e2e/full-stack/token-radar-release.spec.ts",
            "--project=desktop-1366",
        ],
        cwd=_WEB_ROOT,
        env={
            **os.environ,
            "TRACEFOLD_FULL_STACK_URL": base_url,
            "TRACEFOLD_RADAR_BROWSER_READY_PATH": str(ready_path),
            "TRACEFOLD_RADAR_EVIDENCE_PATH": str(evidence_path),
            "TRACEFOLD_RADAR_TIMINGS_PATH": str(timings_path),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for_browser_ready(process, ready_path)
        persisted_at_ms = _seed_minimum_token_radar_facts(postgres_dsn)
        evidence_path.write_text(json.dumps({"persisted_at_ms": persisted_at_ms}), encoding="utf-8")
        _run_token_radar_worker_sample(postgres_dsn)
        output, _ = process.communicate(timeout=_PLAYWRIGHT_TIMEOUT_SECONDS)
        if process.returncode != 0:
            raise AssertionError(f"real Token Radar browser evidence failed:\n{output}")
        if not timings_path.is_file():
            raise AssertionError("real Token Radar browser evidence did not write timing audit")
        timings = json.loads(timings_path.read_text(encoding="utf-8"))
        print(f"TOKEN_RADAR_BROWSER_TIMINGS {json.dumps(timings, sort_keys=True)}")
    finally:
        _stop_process_group(process)


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    """Leave no Playwright browser descendants behind after a lane exits."""
    process_group_id = process.pid
    if _process_group_exists(process_group_id):
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGTERM)
    if process.poll() is None:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
    if _wait_for_process_group_exit(process_group_id, timeout_seconds=1.0):
        return
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGKILL)
    if process.poll() is None:
        process.wait(timeout=5)
    if not _wait_for_process_group_exit(process_group_id, timeout_seconds=5.0):
        raise RuntimeError(f"Playwright process group {process_group_id} survived cleanup")


def _wait_for_process_group_exit(process_group_id: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group_id):
            return True
        time.sleep(0.05)
    return not _process_group_exists(process_group_id)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_browser_ready(process: subprocess.Popen[str], ready_path: Path) -> None:
    deadline = time.monotonic() + _BROWSER_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return
        if process.poll() is not None:
            output, _ = process.communicate()
            raise AssertionError(f"browser release lane exited before polling Radar:\n{output}")
        time.sleep(0.1)
    raise TimeoutError("browser release lane did not reach the real empty Radar route")


def _run_token_radar_worker_sample(postgres_dsn: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tests.e2e._token_radar_worker_entry"],
        cwd=_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(_ROOT / "src"),
            "TRACEFOLD_POSTGRES_DSN": postgres_dsn,
        },
        capture_output=True,
        text=True,
        timeout=_WORKER_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"Token Radar worker sidecar failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    if result.stdout.strip() != "TOKEN_RADAR_SAMPLE_COMPLETED":
        raise AssertionError(f"Token Radar worker sidecar produced unexpected output: {result.stdout!r}")


def _seed_minimum_token_radar_facts(postgres_dsn: str) -> int:
    """Insert the three independent, resolved material facts required by the fixed ruleset."""
    now_ms = int(time.time() * 1_000)
    event_times = (now_ms - 10 * 60_000, now_ms - 8 * 60_000, now_ms - 6 * 60_000)
    with psycopg.connect(postgres_dsn) as connection, connection.transaction():
        for index, received_at_ms in enumerate(event_times, start=1):
            event_id = f"e2e-radar-event-{index}"
            intent_id = f"e2e-radar-intent-{index}"
            resolution_id = f"e2e-radar-resolution-{index}"
            _insert_event(
                connection,
                event_id=event_id,
                author=f"e2e-radar-author-{index}",
                received_at_ms=received_at_ms,
            )
            connection.execute(
                """
                INSERT INTO token_intents(
                  intent_id, event_id, intent_key, construction_policy,
                  primary_evidence_id, display_symbol, display_name,
                  chain_hint, address_hint, intent_status, intent_confidence,
                  created_at_ms, updated_at_ms
                ) VALUES (%s, %s, %s, 'e2e_fixture', NULL, 'E2ERADAR', 'E2E Radar',
                          NULL, NULL, 'resolved', 1.0, %s, %s)
                """,
                (intent_id, event_id, intent_id, received_at_ms, received_at_ms),
            )
            connection.execute(
                """
                INSERT INTO token_intent_resolutions(
                  resolution_id, intent_id, event_id, resolution_status,
                  resolver_policy_version, target_type, target_id, pricefeed_id,
                  reason_codes_json, candidate_ids_json, lookup_keys_json,
                  record_status, is_current, decision_time_ms, created_at_ms
                ) VALUES (%s, %s, %s, 'EXACT', 'e2e_fixture', 'CexToken', 'e2e-radar', NULL,
                          '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                          'current', true, %s, %s)
                """,
                (resolution_id, intent_id, event_id, received_at_ms, received_at_ms),
            )
    # The connection context has committed before this clock is sampled. Using
    # the post-commit wall clock makes the visibility measurement conservative
    # without adding a product-history column solely for a browser test.
    return int(time.time() * 1_000)


def _insert_event(connection: Any, *, event_id: str, author: str, received_at_ms: int) -> None:
    payload = json.dumps({"event_id": event_id})
    connection.execute(
        """
        INSERT INTO events(
          event_id, logical_dedup_key, canonical_url, source_provider,
          source_transport, coverage, channel, action, original_action,
          tweet_id, internal_id, timestamp_ms, received_at_ms,
          author_handle, author_name, author_avatar, author_followers,
          author_tags_json, text, text_raw, text_clean, search_text,
          urls_json, cashtags_json, hashtags_json, mentions_json, media_json,
          reference_json, raw_json, event_json, created_at_ms, updated_at_ms
        ) VALUES (
          %s, %s, NULL, 'e2e', 'direct', 'public_stream', 'e2e', 'tweet', NULL,
          %s, %s, %s, %s, %s, %s, NULL, 1,
          '[]'::jsonb, %s, %s, %s, %s,
          '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
          NULL, %s::jsonb, %s::jsonb, %s, %s
        )
        """,
        (
            event_id,
            event_id,
            event_id,
            event_id,
            received_at_ms,
            received_at_ms,
            author,
            author,
            f"E2ERADAR independent evidence {event_id}",
            f"E2ERADAR independent evidence {event_id}",
            f"E2ERADAR independent evidence {event_id}",
            f"E2ERADAR independent evidence {event_id}",
            payload,
            payload,
            received_at_ms,
            received_at_ms,
        ),
    )
