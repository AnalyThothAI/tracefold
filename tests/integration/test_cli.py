import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tests.support.token_radar import run_token_radar_current
from tracefold.app.cli.parser import build_parser
from tracefold.app.repositories import repositories_for_connection
from tracefold.cli import main
from tracefold.market import (
    Author,
    Content,
    IngestService,
    Source,
    TwitterEvent,
    parse_gmgn_token_payload,
)
from tracefold.platform.config.settings import Settings, default_config_yaml

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
            persisted_live=repos.persisted_live,
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
        run_token_radar_current(conn, now_ms=token_event.received_at_ms + 1)
    finally:
        conn.close()


def write_runtime_config(home: Path, *, db_path: Path, ws_token: str | None = None, llm: bool = False) -> Path:
    app_home = home / ".tracefold"
    app_home.mkdir(parents=True, exist_ok=True)
    payload = {
        "storage": {
            "postgres": {
                "serve_dsn": postgres_test_dsn(),
                "workers_dsn": postgres_test_dsn(),
                "migrate_dsn": postgres_test_dsn(),
                "serve_password_file": None,
                "workers_password_file": None,
                "migrate_password_file": None,
            }
        },
    }
    if ws_token is not None:
        payload["ws_token"] = ws_token
    payload["gmgn"] = {"api_key": "gmgn-test", "openapi_base_url": "https://openapi.gmgn.ai"}
    if llm:
        payload["llm"] = {
            "api_key": "sk-test",
            "base_url": "https://deepseek.test/v1",
            "news_brief_model": "deepseek-chat",
        }
    path = app_home / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


class CliTests(unittest.TestCase):
    def test_runtime_roles_are_explicit_cli_commands(self):
        parser = build_parser()

        assert parser.parse_args(["serve"]).command == "serve"
        assert parser.parse_args(["workers"]).command == "workers"

    def test_workers_runtime_collector_requires_one_bundle_path(self):
        bundle = str(Path.cwd().parent / "runtime-acceptance")
        parsed = build_parser().parse_args(
            [
                "ops",
                "collect-workers-runtime-acceptance",
                "--bundle",
                bundle,
            ]
        )

        assert parsed.ops_command == "collect-workers-runtime-acceptance"
        assert parsed.bundle == bundle

    def test_audit_and_current_token_radar_commands_are_registered(self):
        parser = build_parser()

        commands = [
            ["db", "audit"],
            ["db", "query-audit"],
            ["db", "query-audit", "--analyze"],
            ["ops", "radar-status"],
            ["ops", "validate-projections", "--sample", "5"],
            ["ops", "sync-binance-usdt-perp-universe", "--dry-run"],
            ["ops", "sync-binance-usdt-perp-universe", "--execute"],
            ["ops", "sync-binance-cex-profiles"],
            ["ops", "run-resolution-refresh", "--limit", "5"],
            ["ops", "refresh-asset-profiles", "--limit", "5"],
            ["ops", "mirror-token-images", "--limit", "5"],
            ["ops", "reprocess-token-intents", "--window", "24h", "--limit", "5", "--lookup-key", "symbol:SLOP"],
            ["ops", "rebuild-token-intents", "--window", "5m", "--limit", "5"],
            ["ops", "audit-token-intent", "--event-id", "event-1"],
            ["ops", "sync-us-equity-symbols"],
            ["ops", "seal-workers-runtime-acceptance", "--template"],
        ]

        parsed = [parser.parse_args(command) for command in commands]

        self.assertEqual(parsed[0].db_command, "audit")
        self.assertEqual(parsed[1].db_command, "query-audit")
        self.assertFalse(parsed[1].analyze)
        self.assertTrue(parsed[2].analyze)
        self.assertEqual(parsed[3].ops_command, "radar-status")
        self.assertEqual(parsed[4].ops_command, "validate-projections")
        self.assertEqual(parsed[4].sample, 5)
        self.assertEqual(parsed[5].ops_command, "sync-binance-usdt-perp-universe")
        self.assertTrue(parsed[5].dry_run)
        self.assertEqual(parsed[6].ops_command, "sync-binance-usdt-perp-universe")
        self.assertTrue(parsed[6].execute)
        self.assertEqual(parsed[7].ops_command, "sync-binance-cex-profiles")
        self.assertEqual(parsed[8].ops_command, "run-resolution-refresh")
        self.assertEqual(parsed[8].limit, 5)
        self.assertEqual(parsed[9].ops_command, "refresh-asset-profiles")
        self.assertEqual(parsed[9].limit, 5)
        self.assertEqual(parsed[10].ops_command, "mirror-token-images")
        self.assertEqual(parsed[10].limit, 5)
        self.assertEqual(parsed[11].ops_command, "reprocess-token-intents")
        self.assertEqual(parsed[11].window, "24h")
        self.assertEqual(parsed[11].lookup_key, ["symbol:SLOP"])
        self.assertEqual(parsed[12].ops_command, "rebuild-token-intents")
        self.assertEqual(parsed[12].window, "5m")
        self.assertEqual(parsed[13].ops_command, "audit-token-intent")
        self.assertEqual(parsed[14].ops_command, "sync-us-equity-symbols")
        self.assertEqual(parsed[15].ops_command, "seal-workers-runtime-acceptance")
        self.assertTrue(parsed[15].template)

    def test_workers_runtime_v2_acceptance_template_does_not_require_runtime_config(self):
        stdout = io.StringIO()

        exit_code = main(
            ["ops", "seal-workers-runtime-acceptance", "--template"],
            stdout=stdout,
        )

        assert exit_code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["ok"] is True
        template = payload["data"]["template"]
        assert template["schema_version"] == "workers_runtime_acceptance_v2"
        startup = template["gates"]["startup_recovery"]
        assert "operator_authorized_fix_forward_boundary" in startup
        assert "snapshot_restore" not in startup
        assert template["gates"]["real_continuous_30m"]["status"] == "pending"

    def test_cli_rejects_retired_hard_cut_commands(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "db",
                    "hard-cut",
                    "--bootstrap-dsn",
                    "postgresql://tracefold_app@postgres:5432/tracefold",
                    "--execute",
                ]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(["ops", "hard-cut-rebuild", "--execute"])

    def test_cli_ops_mirror_token_images_has_no_source_limit_option(self):
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["ops", "mirror-token-images", "--source-limit", "9"])

    def test_cli_rejects_retired_projection_queue_commands(self):
        parser = build_parser()

        retired = (
            ["asset-flow", "--window", "1h", "--limit", "5"],
            ["ops", "projection-status"],
            ["ops", "factor-diagnostics", "--window", "1h", "--limit", "200"],
            ["ops", "radar-evaluate"],
            ["ops", "repair-token-profile-images", "--limit", "5"],
            ["ops", "rebuild-token-profiles", "--limit", "5"],
            ["ops", "rebuild-token-radar", "--window", "1h"],
            [
                "ops",
                "enqueue-token-radar-dirty-targets",
                "--source",
                "events",
                "--dry-run",
            ],
        )
        for command in retired:
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(command)

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
        self.assertEqual(
            set(payload["data"]["store"]["postgres_roles"]),
            {"serve", "workers", "migrate"},
        )
        self.assertEqual(payload["data"]["store"]["serve_pool_max_size"], 8)
        self.assertEqual(payload["data"]["store"]["workers_pool_max_size"], 4)
        self.assertNotIn("embed" + "ding_dim", payload["data"]["store"])
        self.assertNotIn("workers", payload["data"])

    def test_generated_default_config_matches_the_current_hard_cut_schema(self):
        payload = yaml.safe_load(default_config_yaml())

        settings = Settings.model_validate(payload)

        self.assertTrue(settings.news.enabled)
        self.assertFalse(settings.news.rss_enabled)
        self.assertFalse(payload["news"]["rss_enabled"])
        self.assertTrue(settings.providers.macro_sources.enabled)
        self.assertTrue(settings.providers.macro_sources.nasdaq_daily_enabled)
        self.assertNotIn("request_timeout_seconds", payload["providers"]["macro_sources"])

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
            "macro source request timeout": {"providers": {"macro_sources": {"request_timeout_seconds": 15}}},
        }

        for label, payload in retired_payloads.items():
            with self.subTest(label=label), self.assertRaises(ValidationError):
                Settings.model_validate(payload)

    def test_settings_reject_worker_runtime_configuration(self):
        with self.assertRaises(ValidationError):
            Settings.model_validate({"workers": {"collector": {"enabled": False}}})

    def test_recent_search_and_radar_status_use_postgres_runtime_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            db_path = home / ".tracefold" / "postgres_test_db"
            write_runtime_config(home, db_path=db_path)
            seed_postgres(db_path)
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                recent_code = main(["recent", "--limit", "5"], stdout=stdout)
                search_code = main(["search", "$PEPE", "--limit", "5"], stdout=stdout)
                radar_status_code = main(["ops", "radar-status"], stdout=stdout)

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            [
                recent_code,
                search_code,
                radar_status_code,
            ],
            [0, 0, 0],
        )
        self.assertEqual(lines[0]["data"]["events"][0]["event_id"], "event-1")
        self.assertEqual(lines[1]["data"]["items"][0]["event"]["event_id"], "event-1")
        self.assertEqual(lines[2]["data"]["schema_version"], "token_radar_snapshot_v2")
        self.assertEqual(lines[2]["data"]["latest_attempt_status"], "ready")
        self.assertEqual(lines[2]["data"]["public_items"], 0)

    def test_db_audit_query_audit_and_token_radar_ops_use_postgres_only(self):
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
                radar_status_code = main(["ops", "radar-status"], stdout=stdout)
                validate_code = main(["ops", "validate-projections", "--sample", "5"], stdout=stdout)

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            [
                db_audit_code,
                query_audit_code,
                radar_status_code,
                validate_code,
            ],
            [0, 0, 0, 0],
        )
        self.assertEqual(lines[0]["data"]["engine"], "postgresql")
        self.assertTrue(lines[0]["data"]["projection_schema"]["token_radar_current"])
        self.assertNotIn("projection_offsets", lines[0]["data"]["projection_schema"])
        self.assertNotIn("projection_runs", lines[0]["data"]["projection_schema"])
        self.assertFalse(lines[1]["data"]["analyze"])
        self.assertIn("token_radar_latest", {item["name"] for item in lines[1]["data"]["queries"]})
        self.assertEqual(lines[2]["data"]["latest_attempt_status"], "never")
        self.assertEqual(lines[2]["data"]["eligible_total"], 0)
        self.assertEqual(lines[2]["data"]["public_items"], 0)
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
    app_home = tmp_path / ".tracefold"
    config_path = app_home / "config.yaml"
    assert config_path.is_file()
    assert app_home.stat().st_mode & 0o777 == 0o700
    assert config_path.stat().st_mode & 0o777 == 0o600
    for directory_name in ("logs", "cache"):
        directory = app_home / directory_name
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700
    for name in (
        "postgres_password",
        "postgres_serve_password",
        "postgres_workers_password",
        "postgres_migrate_password",
    ):
        path = app_home / name
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600


def test_init_is_idempotent_and_does_not_rotate_operator_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first_stdout = io.StringIO()
    second_stdout = io.StringIO()

    assert main(["init"], stdout=first_stdout) == 0
    app_home = tmp_path / ".tracefold"
    tracked_names = (
        "config.yaml",
        "postgres_password",
        "postgres_serve_password",
        "postgres_workers_password",
        "postgres_migrate_password",
    )
    before = {name: (app_home / name).read_bytes() for name in tracked_names}
    app_home.chmod(0o755)
    for name in tracked_names:
        (app_home / name).chmod(0o644)
    for directory_name in ("logs", "cache"):
        (app_home / directory_name).chmod(0o755)

    assert main(["init"], stdout=second_stdout) == 0

    assert json.loads(second_stdout.getvalue())["data"]["created"] is False
    assert {name: (app_home / name).read_bytes() for name in tracked_names} == before
    assert app_home.stat().st_mode & 0o777 == 0o700
    assert all((app_home / name).stat().st_mode & 0o777 == 0o600 for name in tracked_names)
    assert all((app_home / name).stat().st_mode & 0o777 == 0o700 for name in ("logs", "cache"))


def test_cli_runtime_collector_bypasses_maintenance_lock_and_fails_closed(monkeypatch, tmp_path):
    from tracefold.app.cli.commands import ops as ops_module

    write_runtime_config(tmp_path, db_path=tmp_path / ".tracefold" / "postgres_test_db")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        ops_module,
        "collect_workers_runtime_acceptance",
        lambda bundle, settings: {
            "status": "failed",
            "bundle": str(bundle),
            "config_home": str(settings.app_home),
        },
    )

    def maintenance_lock_forbidden(*args, **kwargs):
        raise AssertionError("collector_must_bypass_maintenance_lock")

    monkeypatch.setattr(ops_module.WorkerDatabase, "create", maintenance_lock_forbidden)
    stdout = io.StringIO()

    code = main(
        [
            "ops",
            "collect-workers-runtime-acceptance",
            "--bundle",
            str(tmp_path / "runtime-evidence"),
        ],
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert code == 1
    assert payload["ok"] is False
    assert payload["data"]["status"] == "failed"


if __name__ == "__main__":
    unittest.main()
