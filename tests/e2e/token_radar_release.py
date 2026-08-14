"""Real PostgreSQL, HTTP, and production-browser Token Radar v5 flow."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psycopg

_ROOT = Path(__file__).resolve().parents[2]
_WEB_ROOT = _ROOT / "web"


def run_token_radar_browser_release(*, postgres_dsn: str, base_url: str) -> None:
    """Publish from material facts, then exercise desktop and mobile production UI."""
    _reset_token_radar_current(postgres_dsn)
    _seed_minimum_token_radar_facts(postgres_dsn)
    _run_token_radar_worker_sample(postgres_dsn)

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
            "--project=mobile-390",
        ],
        cwd=_WEB_ROOT,
        env={**os.environ, "TRACEFOLD_FULL_STACK_URL": base_url},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=90)
        if process.returncode != 0:
            raise AssertionError(f"real Token Radar v5 browser flow failed:\n{output}")
    finally:
        _stop_process_group(process)


def _reset_token_radar_current(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection, connection.transaction():
        connection.execute(
            """
            UPDATE token_radar_current
               SET served_payload = jsonb_build_object(
                     'schema_version', 'token_radar_snapshot_v5',
                     'social_evidence_as_of_ms', 0,
                     'eligible_total', 0,
                     'items', jsonb_build_array()
                   ),
                   snapshot_fingerprint =
                     'sha256:5ea0cbe27b8434069c6d9186408f5a372c5290b0c7f4d0f24d68f483df0bd8a8',
                   updated_at_ms = 0
             WHERE singleton_key = true
            """
        )


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
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "TOKEN_RADAR_SAMPLE_COMPLETED":
        raise AssertionError(f"Token Radar worker sidecar failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")


def _seed_minimum_token_radar_facts(postgres_dsn: str) -> None:
    now_ms = int(time.time() * 1_000)
    event_times = (now_ms - 10 * 60_000, now_ms - 8 * 60_000, now_ms - 6 * 60_000)
    asset_id = "e2e-radar-asset"
    chain_id = "solana"
    address = "e2e-radar-mint"
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
              'Asset', %s, 'ready', 'e2e_fixture', 'E2ERADAR', 'E2E Radar', NULL,
              '[]'::jsonb, '{}'::jsonb, %s, %s, 'e2e-radar-profile'
            )
            """,
            (asset_id, now_ms, now_ms),
        )
        for index, source_at_ms in enumerate(event_times, start=1):
            event_id = f"e2e-radar-event-{index}"
            intent_id = f"e2e-radar-intent-{index}"
            resolution_id = f"e2e-radar-resolution-{index}"
            _insert_event(connection, event_id=event_id, author=f"e2e-author-{index}", at_ms=source_at_ms)
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
                (intent_id, event_id, intent_id, source_at_ms, source_at_ms),
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
                (resolution_id, intent_id, event_id, asset_id, source_at_ms, source_at_ms),
            )

        signal_at_ms = event_times[-1]
        current_at_ms = now_ms - 1_000
        _insert_market_tick(
            connection,
            tick_id="e2e-radar-signal-tick",
            at_ms=signal_at_ms,
            chain_id=chain_id,
            address=address,
            price_usd=10,
            market_cap_usd=10_000_000,
        )
        _insert_market_tick(
            connection,
            tick_id="e2e-radar-current-tick",
            at_ms=current_at_ms,
            chain_id=chain_id,
            address=address,
            price_usd=12,
            market_cap_usd=12_000_000,
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
              'tier3_inline', 'e2e_fixture', %s
            )
            """,
            (f"{chain_id}:{address}", signal_at_ms, signal_at_ms, signal_at_ms),
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
            (f"{chain_id}:{address}", current_at_ms, chain_id, address, current_at_ms, current_at_ms),
        )


def _insert_event(connection: psycopg.Connection, *, event_id: str, author: str, at_ms: int) -> None:
    payload = json.dumps({"event_id": event_id})
    text = f"E2ERADAR independent evidence {event_id}"
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
            at_ms,
            at_ms,
            author,
            author,
            text,
            text,
            text,
            text,
            payload,
            payload,
            at_ms,
            at_ms,
        ),
    )


def _insert_market_tick(
    connection: psycopg.Connection,
    *,
    tick_id: str,
    at_ms: int,
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
          %s, %s, %s, '{}'::jsonb, %s, %s
        )
        """,
        (
            at_ms,
            tick_id,
            f"{chain_id}:{address}",
            chain_id,
            address,
            at_ms,
            price_usd,
            market_cap_usd,
            f"{tick_id}-payload",
            at_ms,
        ),
    )


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if _process_group_exists(process.pid):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    if process.poll() is None:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
    if _process_group_exists(process.pid):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.wait(timeout=5)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True
