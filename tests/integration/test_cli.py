import io
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.cli.parser import build_parser
from tracefold.app.repositories import repositories_for_connection
from tracefold.cli import main
from tracefold.market import (
    Author,
    Content,
    IngestService,
    Source,
    TokenRadarProjector,
    TokenRadarPublisher,
    TwitterEvent,
    parse_gmgn_token_payload,
)
from tracefold.platform.config.settings import Settings, WorkersSettings, default_workers_yaml

PEPE = "0x6982508145454ce325ddbe47a25d4ec3d2311933"


def make_event(
    event_id: str,
    received_at_ms: int | None = None,
    text: str = f"$PEPE Solana XDP mainnet base stablecoin {PEPE}",
) -> TwitterEvent:
    received_at_ms = received_at_ms if received_at_ms is not None else int(time.time() * 1000)
    return TwitterEvent(
        event_id=event_id,
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_basic",
        ),
        action="tweet",
        original_action=None,
        tweet_id=event_id,
        internal_id=event_id,
        timestamp=received_at_ms // 1000,
        received_at_ms=received_at_ms,
        author=Author(handle="toly", name="toly", avatar=None, followers=100, tags=[]),
        content=Content(text=text, media=[]),
        reference=None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        raw=None,
    )


def seed_postgres(db_path: Path) -> None:
    conn = connect_postgres_test(db_path, read_only=False)
    try:
        migrate(conn)
        repos = repositories_for_connection(conn)
        ingest = IngestService(
            evidence=repos.evidence,
            entities=repos.entities,
            registry=repos.registry,
            identity_evidence=repos.identity_evidence,
            token_intent_lookup=repos.token_intent_lookup,
            token_evidence=repos.token_evidence,
            token_intents=repos.token_intents,
            intent_resolutions=repos.intent_resolutions,
            discovery=repos.discovery,
            market_ticks=repos.market_ticks,
            market_tick_current=repos.market_tick_current,
            enriched_events=repos.enriched_events,
            event_anchor_jobs=repos.event_anchor_jobs,
            token_radar_dirty_targets=repos.token_radar_dirty_targets,
            transaction=repos.transaction,
            event_anchor_active_window_ms=300_000,
        )
        snapshot = parse_gmgn_token_payload(
            {
                "tt": "ca",
                "t": {
                    "a": PEPE,
                    "c": "eth",
                    "mc": "60490.341996",
                    "p": "1.0",
                    "s": "PEPE",
                },
            }
        )
        token_event = replace(
            make_event("event-1"),
            source=Source(
                provider="gmgn",
                transport="direct_ws",
                coverage="public_stream",
                channel="twitter_monitor_token",
            ),
            token_snapshot=snapshot,
        )
        with repos.transaction():
            ingest.ingest_event(token_event)
            now_ms = token_event.received_at_ms + 1
            repos.token_radar_dirty_targets.enqueue_recent_resolved_targets(
                since_ms=max(0, token_event.received_at_ms - 5 * 60 * 1000),
                now_ms=now_ms,
                limit=20,
                reason="test_seed",
            )
            TokenRadarPublisher(repos=repos, projector=TokenRadarProjector(repos=repos)).rebuild_dirty_targets(
                lease_ms=120_000,
                retry_ms=30_000,
                max_attempts=3,
                windows=("5m",),
                now_ms=now_ms,
                limit=20,
                rank_limit=20,
                lease_owner="test_cli_seed",
            )
    finally:
        conn.close()


def write_runtime_config(home: Path, *, db_path: Path, ws_token: str | None = None, llm: bool = False) -> Path:
    app_home = home / ".tracefold"
    app_home.mkdir(parents=True, exist_ok=True)
    payload = {
        "storage": {"postgres": {"dsn": postgres_test_dsn(), "password_file": None}},
    }
    if ws_token is not None:
        payload["ws_token"] = ws_token
    payload["gmgn"] = {"api_key": "gmgn-test", "openapi_base_url": "https://openapi.gmgn.ai"}
    workers_payload = yaml.safe_load(default_workers_yaml())
    if llm:
        payload["llm"] = {"api_key": "sk-test"}
    path = app_home / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    (app_home / "workers.yaml").write_text(yaml.safe_dump(workers_payload, sort_keys=False), encoding="utf-8")
    return path


class CliTests(unittest.TestCase):
    def test_audit_and_token_radar_projection_commands_are_registered(self):
        parser = build_parser()

        commands = [
            ["db", "audit"],
            ["db", "query-audit"],
            ["db", "query-audit", "--analyze"],
            ["asset-flow", "--window", "1h", "--limit", "5"],
            ["ops", "projection-status"],
            ["ops", "validate-projections", "--sample", "5"],
            ["ops", "sync-binance-usdt-perp-universe", "--dry-run"],
            ["ops", "sync-binance-usdt-perp-universe", "--execute"],
            ["ops", "sync-binance-cex-profiles"],
            ["ops", "run-resolution-refresh", "--limit", "5"],
            ["ops", "refresh-asset-profiles", "--limit", "5"],
            ["ops", "mirror-token-images", "--limit", "5"],
            ["ops", "repair-token-profile-images", "--limit", "5"],
            ["ops", "reprocess-token-intents", "--window", "24h", "--limit", "5", "--lookup-key", "symbol:SLOP"],
            ["ops", "rebuild-token-intents", "--window", "5m", "--limit", "5"],
            ["ops", "audit-token-intent", "--event-id", "event-1"],
            ["ops", "rebuild-token-radar", "--window", "1h"],
            ["ops", "factor-diagnostics", "--window", "1h", "--limit", "200"],
            ["ops", "sync-us-equity-symbols"],
            ["ops", "rebuild-token-profiles", "--limit", "5"],
            ["ops", "enqueue-token-radar-dirty-targets", "--source", "events", "--since-ms", "0", "--dry-run"],
            [
                "ops",
                "enqueue-token-radar-dirty-targets",
                "--source",
                "market-current",
                "--since-ms",
                "0",
                "--execute",
            ],
        ]

        parsed = [parser.parse_args(command) for command in commands]

        self.assertEqual(parsed[0].db_command, "audit")
        self.assertEqual(parsed[1].db_command, "query-audit")
        self.assertFalse(parsed[1].analyze)
        self.assertTrue(parsed[2].analyze)
        self.assertEqual(parsed[3].command, "asset-flow")
        self.assertEqual(parsed[4].ops_command, "projection-status")
        self.assertEqual(parsed[5].ops_command, "validate-projections")
        self.assertEqual(parsed[5].sample, 5)
        self.assertEqual(parsed[6].ops_command, "sync-binance-usdt-perp-universe")
        self.assertTrue(parsed[6].dry_run)
        self.assertEqual(parsed[7].ops_command, "sync-binance-usdt-perp-universe")
        self.assertTrue(parsed[7].execute)
        self.assertEqual(parsed[8].ops_command, "sync-binance-cex-profiles")
        self.assertEqual(parsed[9].ops_command, "run-resolution-refresh")
        self.assertEqual(parsed[9].limit, 5)
        self.assertEqual(parsed[10].ops_command, "refresh-asset-profiles")
        self.assertEqual(parsed[10].limit, 5)
        self.assertEqual(parsed[11].ops_command, "mirror-token-images")
        self.assertEqual(parsed[11].limit, 5)
        self.assertEqual(parsed[12].ops_command, "repair-token-profile-images")
        self.assertEqual(parsed[12].limit, 5)
        self.assertEqual(parsed[13].ops_command, "reprocess-token-intents")
        self.assertEqual(parsed[13].window, "24h")
        self.assertEqual(parsed[13].lookup_key, ["symbol:SLOP"])
        self.assertEqual(parsed[14].ops_command, "rebuild-token-intents")
        self.assertEqual(parsed[14].window, "5m")
        self.assertEqual(parsed[15].ops_command, "audit-token-intent")
        self.assertEqual(parsed[16].ops_command, "rebuild-token-radar")
        self.assertEqual(parsed[17].ops_command, "factor-diagnostics")
        self.assertEqual(parsed[17].limit, 200)
        self.assertEqual(parsed[18].ops_command, "sync-us-equity-symbols")
        self.assertEqual(parsed[19].ops_command, "rebuild-token-profiles")
        self.assertEqual(parsed[19].limit, 5)
        self.assertEqual(parsed[20].ops_command, "enqueue-token-radar-dirty-targets")
        self.assertEqual(parsed[20].source, "events")
        self.assertEqual(parsed[20].since_ms, 0)
        self.assertEqual(parsed[20].limit, 5000)
        self.assertTrue(parsed[20].dry_run)
        self.assertEqual(parsed[21].ops_command, "enqueue-token-radar-dirty-targets")
        self.assertEqual(parsed[21].source, "market-current")
        self.assertTrue(parsed[21].execute)

    def test_cli_ops_mirror_token_images_has_no_source_limit_option(self):
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["ops", "mirror-token-images", "--source-limit", "9"])

    def test_cli_ops_repair_token_profile_images_rejects_non_positive_limit(self):
        parser = build_parser()

        for limit in ("0", "-1"):
            with self.subTest(limit=limit), self.assertRaises(SystemExit):
                parser.parse_args(["ops", "repair-token-profile-images", "--limit", limit])

    def test_config_prints_effective_runtime_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            db_path = home / ".tracefold" / "postgres_test_db"
            write_runtime_config(home, db_path=db_path, ws_token="secret", llm=True)
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                exit_code = main(["config"], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertNotIn("handles", payload["data"])
        self.assertNotIn("handle_count", payload["data"])
        self.assertTrue(payload["data"]["api"]["ws_token_configured"])
        self.assertEqual(
            payload["data"]["config_path"],
            str(home / ".tracefold" / "config.yaml"),
        )
        self.assertNotIn("agent_execution", payload["data"])
        self.assertNotIn("llm", payload["data"])
        self.assertEqual(
            payload["data"]["providers"]["gmgn"],
            {
                "configured": True,
                "openapi_base_url": "https://openapi.gmgn.ai",
                "timeout_seconds": 5.0,
                "token_info_cache_ttl_seconds": 60,
            },
        )
        self.assertNotIn("gmgn-test", stdout.getvalue())
        self.assertEqual(payload["data"]["store"]["engine"], "postgresql")
        self.assertIn("postgres_dsn", payload["data"]["store"])
        self.assertNotIn("embed" + "ding_dim", payload["data"]["store"])
        self.assertEqual(
            payload["data"]["workers_config_path"],
            str(home / ".tracefold" / "workers.yaml"),
        )
        self.assertEqual(payload["data"]["workers"]["collector"]["mode"], "continuous")
        self.assertTrue(payload["data"]["workers"]["collector"]["enabled"])

    def test_config_example_matches_the_current_hard_cut_schema(self):
        example_path = Path(__file__).resolve().parents[2] / "config.example.yaml"
        payload = yaml.safe_load(example_path.read_text(encoding="utf-8"))

        settings = Settings.model_validate(payload)

        self.assertTrue(settings.news.enabled)
        self.assertTrue(settings.providers.macro_sources.enabled)

    def test_settings_reject_retired_watchlist_notification_and_news_source_config(self):
        retired_payloads = {
            "watchlist handles": {"handles": ["wallstengine"]},
            "notifications": {"notifications": {"enabled": True}},
            "configured news sources": {
                "news": {
                    "sources": [
                        {
                            "source_id": "wallstengine",
                            "feed_url": "https://example.invalid/rss",
                        }
                    ]
                }
            },
        }

        for label, payload in retired_payloads.items():
            with self.subTest(label=label), self.assertRaises(ValidationError):
                Settings.model_validate(payload)

    def test_workers_settings_reject_retired_notification_and_radar_scope_config(self):
        retired_payloads = {
            "notification rule worker": {"notification_rule": {"enabled": True}},
            "notification delivery worker": {"notification_delivery": {"enabled": True}},
            "radar scopes": {
                "token_radar_projection": {
                    "scopes": ["all", "matched"],
                }
            },
        }

        for label, payload in retired_payloads.items():
            with self.subTest(label=label), self.assertRaises(ValidationError):
                WorkersSettings.model_validate(payload)

    def test_recent_search_and_asset_flow_use_postgres_runtime_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            db_path = home / ".tracefold" / "postgres_test_db"
            write_runtime_config(home, db_path=db_path)
            seed_postgres(db_path)
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                recent_code = main(["recent", "--limit", "5"], stdout=stdout)
                search_code = main(["search", "$PEPE", "--limit", "5"], stdout=stdout)
                asset_flow_code = main(
                    ["asset-flow", "--window", "5m", "--limit", "5"],
                    stdout=stdout,
                )

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            [
                recent_code,
                search_code,
                asset_flow_code,
            ],
            [0, 0, 0],
        )
        self.assertEqual(lines[0]["data"]["events"][0]["event_id"], "event-1")
        self.assertEqual(lines[1]["data"]["items"][0]["event"]["event_id"], "event-1")
        self.assertNotIn("scope", lines[2]["data"])
        factor_snapshot = lines[2]["data"]["targets"][0]["factor_snapshot"]
        self.assertEqual(factor_snapshot["subject"]["symbol"], "PEPE")
        self.assertEqual(factor_snapshot["families"]["social_heat"]["facts"]["mentions_5m"], 1)

    def test_db_audit_query_audit_and_token_radar_projection_ops_use_postgres_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            db_path = home / ".tracefold" / "postgres_test_db"
            write_runtime_config(home, db_path=db_path)
            conn = connect_postgres_test(db_path, read_only=False)
            try:
                migrate(conn)
            finally:
                conn.close()
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                db_audit_code = main(["db", "audit"], stdout=stdout)
                query_audit_code = main(["db", "query-audit"], stdout=stdout)
                projection_status_code = main(["ops", "projection-status"], stdout=stdout)
                validate_code = main(["ops", "validate-projections", "--sample", "5"], stdout=stdout)

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([db_audit_code, query_audit_code, projection_status_code, validate_code], [0, 0, 0, 0])
        self.assertEqual(lines[0]["data"]["engine"], "postgresql")
        self.assertTrue(lines[0]["data"]["projection_schema"]["token_radar_publication_state"])
        self.assertNotIn("projection_offsets", lines[0]["data"]["projection_schema"])
        self.assertNotIn("projection_runs", lines[0]["data"]["projection_schema"])
        self.assertFalse(lines[1]["data"]["analyze"])
        self.assertIn("token_radar_latest", {item["name"] for item in lines[1]["data"]["queries"]})
        self.assertEqual(lines[2]["data"]["status"], "missing")
        self.assertEqual(lines[2]["data"]["state_count"], 0)
        self.assertEqual(lines[2]["data"]["publication_states"], [])
        self.assertEqual(lines[3]["data"]["sample"], 5)
        self.assertEqual(lines[3]["data"]["mismatch_count"], 0)


def test_recent_defaults_to_runtime_postgres_store_without_ws_token(tmp_path, monkeypatch):
    app_home = tmp_path / ".tracefold"
    db_path = app_home / "postgres_test_db"
    write_runtime_config(tmp_path, db_path=db_path)
    seed_postgres(db_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    stdout = io.StringIO()

    exit_code = main(["recent", "--limit", "5"], stdout=stdout)

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["data"]["events"][0]["event_id"] == "event-1"


def test_init_creates_runtime_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stdout = io.StringIO()

    exit_code = main(["init"], stdout=stdout)

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["data"]["created"] is True
    assert (tmp_path / ".tracefold" / "config.yaml").is_file()


def test_cli_ops_factor_diagnostics_reads_latest_token_radar_current_rows(monkeypatch, tmp_path):
    from tracefold.app.cli.commands import ops as ops_module
    from tracefold.market import (
        TOKEN_FACTOR_SNAPSHOT_VERSION,
        TOKEN_RADAR_FACTOR_FAMILIES,
        TOKEN_RADAR_PROJECTION_VERSION,
    )

    captured = {}

    class FakeTokenRadar:
        def latest_current_rows(self, **kwargs):
            captured.update(kwargs)
            return [
                {
                    "factor_snapshot_json": {
                        "schema_version": TOKEN_FACTOR_SNAPSHOT_VERSION,
                        "families": {
                            family: {"score": 50, "data_health": "ready", "facts": {}, "factors": {}}
                            for family in TOKEN_RADAR_FACTOR_FAMILIES
                        },
                        "gates": {"eligible_for_high_alert": True, "blocked_reasons": []},
                        "data_health": {"identity": "ready", "market": "ready", "social": "ready", "alpha": "ready"},
                        "normalization": {},
                        "composite": {"rank_score": 50, "recommended_decision": "watch"},
                    }
                }
            ]

    class FakeRepos:
        token_radar = FakeTokenRadar()

    @contextmanager
    def fake_repositories(_settings):
        yield FakeRepos()

    write_runtime_config(tmp_path, db_path=tmp_path / ".tracefold" / "postgres_test_db")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ops_module, "repositories", fake_repositories)
    stdout = io.StringIO()

    code = main(
        ["ops", "factor-diagnostics", "--window", "1h", "--limit", "7"],
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert code == 0
    assert captured == {
        "window": "1h",
        "venue": "all",
        "limit": 7,
        "projection_version": TOKEN_RADAR_PROJECTION_VERSION,
    }
    assert payload["ok"] is True
    assert payload["data"]["row_count"] == 1


if __name__ == "__main__":
    unittest.main()
