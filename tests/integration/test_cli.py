import asyncio
import io
import json
import os
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.cli.parser import build_parser
from tracefold.cli import main
from tracefold.platform.config.loader import default_config_yaml
from tracefold.platform.config.models import Settings

NEWS_V3_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"


def _amqp_reachable(url: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5672
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _delete_test_topology(url: str, name_prefix: str) -> None:
    """The bus-check test declares a prefixed topology on the shared development broker; leave nothing behind."""
    from tracefold.integrations.rabbitmq import RabbitMQBus

    async def _run() -> None:
        bus = RabbitMQBus(url=url, name_prefix=name_prefix, connect_timeout_seconds=5.0)
        try:
            await bus.connect()
            await bus.delete_topology()
        finally:
            await bus.close()

    asyncio.run(_run())


def write_runtime_config(
    home: Path,
    *,
    db_path: Path,
    ws_token: str | None = None,
    llm: bool = False,
    opennews_token: str | None = None,
) -> Path:
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
    if llm:
        payload["llm"] = {
            "api_key": "sk-test",
            "base_url": "https://deepseek.test/v1",
            "news_triage_model": "deepseek-chat",
        }
    if opennews_token is not None:
        payload["news"] = {"opennews_token": opennews_token}
    path = app_home / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


class CliTests(unittest.TestCase):
    def test_init_rejects_a_password_path_that_is_a_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            app_home = home / ".tracefold"
            (app_home / "postgres_serve_password").mkdir(parents=True)
            with (
                patch.dict("os.environ", {"HOME": str(home)}, clear=False),
                self.assertRaisesRegex(ValueError, "postgres_password_path_not_file:postgres_serve_password"),
            ):
                main(["init"], stdout=io.StringIO())

    def test_runtime_roles_are_explicit_cli_commands(self):
        parser = build_parser()

        assert parser.parse_args(["serve"]).command == "serve"
        assert parser.parse_args(["workers"]).command == "workers"

    def test_audit_and_current_operations_commands_are_registered(self):
        parser = build_parser()

        commands = [
            ["db", "audit"],
            ["db", "query-audit"],
            ["db", "query-audit", "--analyze"],
            ["ops", "validate-projections", "--sample", "5"],
            ["news", "review", "queue", "--event", "ev-1", "--limit", "5"],
            ["news", "dlq", "inspect", "--limit", "5"],
            ["news", "review", "evidence", "evt.ev-1.1.0123456789abcdef", "--version", "a" * 64],
        ]

        parsed = [parser.parse_args(command) for command in commands]

        self.assertEqual(parsed[0].db_command, "audit")
        self.assertEqual(parsed[1].db_command, "query-audit")
        self.assertFalse(parsed[1].analyze)
        self.assertTrue(parsed[2].analyze)
        self.assertEqual(parsed[3].ops_command, "validate-projections")
        self.assertEqual(parsed[3].sample, 5)
        self.assertEqual((parsed[4].news_command, parsed[4].review_command), ("review", "queue"))
        self.assertEqual((parsed[4].event, parsed[4].limit), ("ev-1", 5))
        self.assertEqual((parsed[5].news_command, parsed[5].dlq_action, parsed[5].limit), ("dlq", "inspect", 5))
        self.assertEqual((parsed[6].news_command, parsed[6].review_command), ("review", "evidence"))
        self.assertEqual((parsed[6].task, parsed[6].version), ("evt.ev-1.1.0123456789abcdef", "a" * 64))

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
        for command in (
            ["news", "label", "ev-1", "noise"],
            ["news", "eval"],
            ["news", "replay-decisions"],
            ["news", "corpus", "freeze"],
            ["news", "validate-candidate"],
        ):
            with self.assertRaises(SystemExit):
                parser.parse_args(command)

    def test_cli_rejects_retired_projection_queue_commands(self):
        parser = build_parser()

        retired = (
            ["asset-flow", "--window", "1h", "--limit", "5"],
            ["ops", "projection-status"],
            ["ops", "collect-workers-runtime-acceptance", "--bundle", "x"],
            ["ops", "seal-workers-runtime-acceptance", "--template"],
            ["ops", "refresh-asset-profiles", "--limit", "5"],
            ["ops", "mirror-token-images", "--limit", "5"],
            ["ops", "run-resolution-refresh", "--limit", "5"],
            ["ops", "sync-binance-cex-profiles"],
            ["ops", "sync-binance-usdt-perp-universe", "--dry-run"],
            ["ops", "sync-us-equity-symbols"],
            ["ops", "rebuild-token-intents", "--window", "5m", "--limit", "5"],
            ["ops", "audit-token-intent", "--event-id", "event-1"],
            ["ops", "rebuild-market-current", "--execute"],
            ["ops", "reconcile-event-anchor-jobs"],
            ["ops", "reprocess-token-intents", "--window", "24h", "--limit", "5"],
            ["recent", "--limit", "5"],
            ["search", "btc"],
            ["news", "control", "drain"],
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
            write_runtime_config(
                home,
                db_path=db_path,
                ws_token="secret",
                llm=True,
                opennews_token="opennews-secret",
            )
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
        self.assertNotIn("providers", payload["data"])
        self.assertNotIn("macro", payload["data"])
        self.assertNotIn("upstream", payload["data"])
        news = payload["data"]["news"]
        self.assertTrue(news["opennews_token_configured"])
        self.assertNotIn("opennews_strategy_ids", news)
        self.assertNotIn("opennews_token", news)
        self.assertEqual(
            set(news),
            {
                "enabled",
                "opennews_token_configured",
                "broker",
                "models",
                "triage",
                "watchlist",
                "policy",
                "retention",
                "gate",
                "push",
            },
        )
        self.assertEqual(set(news["broker"]), {"url_configured", "name_prefix"})
        self.assertEqual(
            news["policy"],
            {
                "listing_exempt_from_duplicate": True,
                "restatement_drop": True,
                "similarity_max": 0.25,
                "stale_source_max_age_s": 43_200,
            },
        )
        self.assertIs(news["policy"]["restatement_drop"], True)
        self.assertEqual(news["retention"], {"raw_days": 30, "judged_days": 365})
        self.assertIs(news["gate"]["suppress_low_signal"], False)
        self.assertFalse(news["broker"]["url_configured"])
        self.assertTrue(news["models"]["triage_configured"])
        self.assertEqual(news["models"]["triage_model"], "deepseek-chat")
        self.assertEqual(news["models"]["reader_card_model"], "deepseek-chat")
        self.assertIs(news["models"]["reader_card_dedicated"], False)
        self.assertIsNone(news["models"]["reader_card_fallback_model"])
        self.assertIs(news["models"]["reader_card_fallback_dedicated"], False)
        self.assertIsInstance(news["watchlist"], list)
        self.assertNotIn("hourly_cap", news["push"])
        self.assertNotIn("rss_enabled", news)
        self.assertNotIn("brief", news)
        self.assertNotIn("title_presentation", news)
        self.assertNotIn("private-strategy-alpha", stdout.getvalue())
        self.assertNotIn("private-strategy-beta", stdout.getvalue())
        self.assertEqual(payload["data"]["store"]["engine"], "postgresql")
        self.assertEqual(
            set(payload["data"]["store"]["postgres_roles"]),
            {"serve", "workers", "migrate"},
        )
        self.assertEqual(payload["data"]["store"]["serve_pool_max_size"], 7)
        self.assertEqual(payload["data"]["store"]["workers_pool_max_size"], 8)
        self.assertNotIn("embed" + "ding_dim", payload["data"]["store"])
        self.assertNotIn("workers", payload["data"])

    def test_generated_default_config_matches_the_current_hard_cut_schema(self):
        payload = yaml.safe_load(default_config_yaml())

        settings = Settings.model_validate(payload)

        self.assertTrue(settings.news.enabled)
        self.assertNotIn("rss_enabled", payload["news"])
        self.assertNotIn("title_presentation", payload["news"])
        self.assertNotIn("news_brief_model", payload.get("llm") or {})
        self.assertEqual(
            payload["llm"]["news_reader_card"],
            {"api_key": None, "base_url": None, "model": None},
        )
        self.assertEqual(
            payload["llm"]["news_reader_card_fallback"],
            {"api_key": None, "base_url": None, "model": None},
        )
        self.assertNotIn("opennews_strategy_ids", payload["news"])
        self.assertEqual(payload["news"]["broker"]["url"], "amqp://tracefold:tracefold@rabbitmq:5672/")
        self.assertEqual(settings.news.broker.name_prefix, "")
        self.assertFalse(settings.news.push.enabled)
        self.assertNotIn("providers", payload)
        self.assertNotIn("macro_document_analysis_enabled", payload["llm"])
        self.assertEqual(set(payload), {"ws_token", "api", "storage", "llm", "news"})

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
            "retired macro sources": {"providers": {"macro_sources": {"enabled": True}}},
            "retired macro document analysis": {"llm": {"macro_document_analysis_enabled": True}},
            "gmgn stream": {"upstream": {"chains": ["sol"]}},
            "gmgn openapi": {"gmgn": {"api_key": "x"}},
            "binance": {"providers": {"binance": {"enabled": True}}},
            "websocket replay": {"api": {"replay_limit": 10}},
        }

        for label, payload in retired_payloads.items():
            with self.subTest(label=label), self.assertRaises(ValidationError):
                Settings.model_validate(payload)

    def test_settings_reject_worker_runtime_configuration(self):
        with self.assertRaises(ValidationError):
            Settings.model_validate({"workers": {"collector": {"enabled": False}}})

    def test_db_audit_query_audit_and_validate_projections_use_postgres_only(self):
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
                validate_code = main(["ops", "validate-projections", "--sample", "5"], stdout=stdout)

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            [
                db_audit_code,
                query_audit_code,
                validate_code,
            ],
            [0, 0, 0],
            lines,
        )
        self.assertEqual(lines[0]["data"]["engine"], "postgresql")
        self.assertTrue(lines[0]["data"]["news_schema"]["exact"])
        self.assertNotIn("projection_schema", lines[0]["data"])
        self.assertFalse(lines[1]["data"]["analyze"])
        self.assertNotIn("token_radar_latest", {item["name"] for item in lines[1]["data"]["queries"]})
        self.assertEqual(lines[2]["data"]["sample"], 5)
        self.assertEqual(lines[2]["data"]["mismatch_count"], 0)

    def test_news_replay_reports_offline_gate_counts_from_a_hits_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            write_runtime_config(home, db_path=home / ".tracefold" / "postgres_test_db")
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                exit_code = main(["news", "replay", str(NEWS_V3_FIXTURE)], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        report = payload["data"]
        self.assertGreater(report["counts"]["items"], 0)
        self.assertGreater(report["counts"]["events"], 0)
        self.assertLessEqual(report["counts"]["events"], report["counts"]["items"])
        self.assertIn("candidate_share_of_items", report)
        self.assertIsInstance(report["sample_candidates"], list)

    def test_news_bus_check_reports_topology_or_fails_closed_without_broker(self):
        amqp_url = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "amqp://tracefold:tracefold@127.0.0.1:5672/")
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            config_path = write_runtime_config(home, db_path=home / ".tracefold" / "postgres_test_db")
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                missing_code = main(["news", "bus-check"], stdout=stdout)
            missing_payload = json.loads(stdout.getvalue())
            self.assertEqual(missing_code, 1)
            self.assertFalse(missing_payload["ok"])
            self.assertEqual(missing_payload["error"], "ValueError")
            self.assertIn("news_broker_url_missing", missing_payload["detail"])

            if not _amqp_reachable(amqp_url):
                self.skipTest("development RabbitMQ is not reachable")
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            name_prefix = f"tf_test_{uuid.uuid4().hex[:8]}"
            payload["news"] = {
                **(payload.get("news") or {}),
                "broker": {"url": amqp_url, "name_prefix": name_prefix},
            }
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
            stdout = io.StringIO()
            try:
                with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                    exit_code = main(["news", "bus-check"], stdout=stdout)
            finally:
                _delete_test_topology(amqp_url, name_prefix)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("queues", payload["data"])
        self.assertIn("declared", payload["data"])
        self.assertNotIn("guest:guest", stdout.getvalue())


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


if __name__ == "__main__":
    unittest.main()
