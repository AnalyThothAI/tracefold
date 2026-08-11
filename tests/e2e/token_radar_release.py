"""Test-only orchestration for real Token Radar browser release evidence."""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from hashlib import sha256
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
    app_home: Path,
) -> None:
    """Prove one persisted fact reaches the real served browser without mocks."""
    _reset_token_radar_current(postgres_dsn)
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
        persisted_at_ms = _seed_minimum_token_radar_facts(postgres_dsn, app_home=app_home)
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


def _reset_token_radar_current(postgres_dsn: str) -> None:
    """Give this browser lane the same unavailable singleton as the v3 hard cut."""
    with psycopg.connect(postgres_dsn) as connection, connection.transaction():
        connection.execute(
            """
            UPDATE token_radar_current
               SET schema_version = 'token_radar_snapshot_v3',
                   ruleset_version = NULL,
                   ruleset_fingerprint = NULL,
                   input_fingerprint = NULL,
                   state_fingerprint = NULL,
                   evidence_as_of_ms = 0,
                   evaluation_at_ms = 0,
                   input_rows = 0,
                   input_bytes = 0,
                   latest_attempt_status = 'never',
                   latest_error_code = NULL,
                   failure_count = 0,
                   state_changed_at_ms = 0,
                   served_payload = jsonb_build_object(
                     'schema_version', 'token_radar_snapshot_v3',
                     'social_evidence_as_of_ms', 0,
                     'eligible_total', 0,
                     'items', jsonb_build_array()
                   ),
                   created_at_ms = 0,
                   updated_at_ms = 0
             WHERE singleton_key = true
            """
        )


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


def _seed_minimum_token_radar_facts(postgres_dsn: str, *, app_home: Path) -> int:
    """Insert the minimum resolved facts plus real profile and market presentation facts."""
    now_ms = int(time.time() * 1_000)
    event_times = (now_ms - 10 * 60_000, now_ms - 8 * 60_000, now_ms - 6 * 60_000)
    asset_id = "e2e-radar-asset"
    chain_id = "solana"
    address = "e2e-radar-mint"
    image_id = _write_e2e_token_image(app_home)
    with psycopg.connect(postgres_dsn) as connection, connection.transaction():
        connection.execute(
            """
            INSERT INTO registry_assets(
              asset_id, chain_id, token_standard, address, status,
              first_seen_at_ms, updated_at_ms
            ) VALUES (%s, %s, 'spl', %s, 'canonical', %s, %s)
            """,
            (asset_id, chain_id, address, event_times[0], now_ms),
        )
        connection.execute(
            """
            INSERT INTO asset_identity_current(
              asset_id, canonical_symbol, canonical_name, decimals,
              identity_confidence, selection_reason_codes_json,
              conflict_count, verified_at_ms, updated_at_ms
            ) VALUES (%s, 'E2ERADAR', 'E2E Radar', 9, 'high', '[]'::jsonb, 0, %s, %s)
            """,
            (asset_id, now_ms, now_ms),
        )
        connection.execute(
            """
            INSERT INTO token_profile_current(
              target_type, target_id, status, source_kind, symbol, name, logo_url,
              quality_flags_json, source_payload_json, computed_at_ms, updated_at_ms,
              payload_hash
            ) VALUES (
              'Asset', %s, 'ready', 'e2e_fixture', 'E2ERADAR', 'E2E Radar', %s,
              '[]'::jsonb, '{}'::jsonb, %s, %s, 'e2e-radar-profile'
            )
            """,
            (asset_id, f"/api/token-images/{image_id}", now_ms, now_ms),
        )
        image_path = f"{image_id}.png"
        connection.execute(
            """
            INSERT INTO token_image_assets(
              image_id, source_url, source_url_hash, source_provider, source_kind,
              status, media_type, file_extension, content_sha256, byte_size,
              storage_path, public_url, raw_ref_json, next_refresh_at_ms,
              created_at_ms, updated_at_ms
            ) VALUES (
              %s, 'https://e2e.invalid/radar.png', %s, 'e2e', 'profile',
              'ready', 'image/png', '.png', %s, %s,
              %s, %s, '{}'::jsonb, %s, %s, %s
            )
            """,
            (
                image_id,
                image_id,
                _E2E_TOKEN_IMAGE_CONTENT_SHA256,
                len(_E2E_TOKEN_IMAGE_BYTES),
                image_path,
                f"/api/token-images/{image_id}",
                now_ms + 86_400_000,
                now_ms,
                now_ms,
            ),
        )
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
                ) VALUES (%s, %s, %s, 'EXACT', 'e2e_fixture', 'Asset', %s, NULL,
                          '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                          'current', true, %s, %s)
                """,
                (resolution_id, intent_id, event_id, asset_id, received_at_ms, received_at_ms),
            )
        signal_observed_at_ms = event_times[-1]
        _insert_e2e_market_tick(
            connection,
            observed_at_ms=signal_observed_at_ms,
            tick_id="e2e-radar-signal-tick",
            chain_id=chain_id,
            address=address,
            price_usd=10,
            market_cap_usd=10_000_000,
        )
        connection.execute(
            """
            INSERT INTO enriched_events(
              event_id, intent_id, resolution_id, target_type, target_id,
              t_event_ms, tick_observed_at_ms, tick_id, tick_lag_ms,
              capture_method, capture_reason, created_at_ms
            ) VALUES (
              'e2e-radar-event-3', 'e2e-radar-intent-3', 'e2e-radar-resolution-3',
              'chain_token', %s, %s, %s, 'e2e-radar-signal-tick', 0,
              'tier3_inline', 'fresh_tick', %s
            )
            """,
            (
                f"{chain_id}:{address}",
                signal_observed_at_ms,
                signal_observed_at_ms,
                signal_observed_at_ms,
            ),
        )
        current_observed_at_ms = now_ms - 1_000
        _insert_e2e_market_tick(
            connection,
            observed_at_ms=current_observed_at_ms,
            tick_id="e2e-radar-current-tick",
            chain_id=chain_id,
            address=address,
            price_usd=12,
            market_cap_usd=12_000_000,
        )
        connection.execute(
            """
            INSERT INTO market_tick_current(
              target_type, target_id, tick_observed_at_ms, tick_id,
              source_tier, source_provider, chain, token_address,
              price_usd, market_cap_usd, updated_at_ms, created_at_ms
            ) VALUES (
              'chain_token', %s, %s, 'e2e-radar-current-tick',
              'tier3_inline', 'gmgn_dex_quote', %s, %s,
              12, 12000000, %s, %s
            )
            """,
            (
                f"{chain_id}:{address}",
                current_observed_at_ms,
                chain_id,
                address,
                current_observed_at_ms,
                current_observed_at_ms,
            ),
        )
    # The connection context has committed before this clock is sampled. Using
    # the post-commit wall clock makes the visibility measurement conservative
    # without adding a product-history column solely for a browser test.
    return int(time.time() * 1_000)


_E2E_TOKEN_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_E2E_TOKEN_IMAGE_CONTENT_SHA256 = sha256(_E2E_TOKEN_IMAGE_BYTES).hexdigest()


def _write_e2e_token_image(app_home: Path) -> str:
    source_url = "https://e2e.invalid/radar.png"
    image_id = sha256(source_url.encode("utf-8")).hexdigest()
    cache_dir = app_home / "cache" / "token-images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{image_id}.png").write_bytes(_E2E_TOKEN_IMAGE_BYTES)
    return image_id


def _insert_e2e_market_tick(
    connection: Any,
    *,
    observed_at_ms: int,
    tick_id: str,
    chain_id: str,
    address: str,
    price_usd: int,
    market_cap_usd: int,
) -> None:
    connection.execute(
        """
        INSERT INTO market_ticks(
          observed_at_ms, tick_id, target_type, target_id,
          chain, token_address, source_tier, source_provider,
          received_at_ms, price_usd, market_cap_usd,
          raw_payload_json, payload_hash, created_at_ms
        ) VALUES (
          %s, %s, 'chain_token', %s,
          %s, %s, 'tier3_inline', 'gmgn_dex_quote',
          %s, %s, %s,
          '{}'::jsonb, %s, %s
        )
        """,
        (
            observed_at_ms,
            tick_id,
            f"{chain_id}:{address}",
            chain_id,
            address,
            observed_at_ms,
            price_usd,
            market_cap_usd,
            f"{tick_id}-payload",
            observed_at_ms,
        ),
    )


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
          %s, %s, NULL, 'gmgn', 'direct_ws', 'public_stream',
          'twitter_monitor_basic', 'tweet', NULL,
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
