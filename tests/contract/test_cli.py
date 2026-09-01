import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from tracefold.app.cli.parser import build_parser
from tracefold.app.cli.parsers.database import add_database_commands
from tracefold.app.cli.parsers.news import add_news_commands
from tracefold.app.cli.parsers.ops import add_ops_commands
from tracefold.app.cli.parsers.runtime import add_runtime_commands
from tracefold.app.cli.parsers.trading import add_trading_commands
from tracefold.cli import main
from tracefold.platform.config.loader import default_config_yaml
from tracefold.platform.config.models import Settings

NEWS_V3_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"


def write_runtime_config(
    home: Path,
    *,
    postgres_dsn: str = "postgresql://postgres:postgres@127.0.0.1:55432/tracefold_test",
    ws_token: str | None = None,
    llm: bool = False,
    opennews_token: str | None = None,
) -> Path:
    app_home = home / ".tracefold"
    app_home.mkdir(parents=True, exist_ok=True)
    payload = {
        "storage": {"postgres": {"dsn": postgres_dsn, "password_file": None}},
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
    def test_init_rejects_a_telegram_token_path_that_is_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            app_home = home / ".tracefold"
            app_home.mkdir(parents=True)
            target = app_home / "unexpected_target"
            target.write_text("do-not-touch", encoding="utf-8")
            (app_home / "telegram_bot_token").symlink_to(target)
            with (
                patch.dict("os.environ", {"HOME": str(home)}, clear=False),
                self.assertRaisesRegex(ValueError, "optional_secret_path_not_file:telegram_bot_token"),
            ):
                main(["init"], stdout=io.StringIO())
            self.assertEqual(target.read_text(encoding="utf-8"), "do-not-touch")

    def test_init_rejects_a_password_path_that_is_a_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            app_home = home / ".tracefold"
            (app_home / "postgres_database_password").mkdir(parents=True)
            with (
                patch.dict("os.environ", {"HOME": str(home)}, clear=False),
                self.assertRaisesRegex(ValueError, "postgres_password_path_not_file:postgres_database_password"),
            ):
                main(["init"], stdout=io.StringIO())

    def test_runtime_processes_are_explicit_cli_commands(self):
        parser = build_parser()

        assert parser.parse_args(["serve"]).command == "serve"
        assert parser.parse_args(["workers"]).command == "workers"
        nautilus = parser.parse_args(["nautilus", "run"])
        assert (nautilus.command, nautilus.nautilus_command) == ("nautilus", "run")

    def test_representative_command_namespaces_are_stable(self):
        parser = build_parser()
        cases = (
            (
                ["nautilus", "run"],
                {"command": "nautilus", "nautilus_command": "run"},
            ),
            (["db", "audit", "--deep"], {"command": "db", "db_command": "audit", "deep": True}),
            (
                ["news", "instruments", "unmatched", "--symbol", "BTC", "--days", "3", "--limit", "7"],
                {
                    "command": "news",
                    "news_command": "instruments",
                    "action": "unmatched",
                    "symbol": "BTC",
                    "days": 3,
                    "limit": 7,
                },
            ),
            (
                ["trading", "signals", "--limit", "7"],
                {
                    "command": "trading",
                    "trading_command": "signals",
                    "limit": 7,
                },
            ),
            (
                [
                    "trading",
                    "issue",
                    "/pause maintenance",
                    "--request-id",
                    "ops-20260901-1",
                    "--requested-at-ns",
                    "1788218708000000000",
                ],
                {
                    "command": "trading",
                    "trading_command": "issue",
                    "text": "/pause maintenance",
                    "request_id": "ops-20260901-1",
                    "requested_at_ns": 1788218708000000000,
                },
            ),
            (
                ["ops", "validate-projections", "--sample", "5"],
                {"command": "ops", "ops_command": "validate-projections", "sample": 5},
            ),
        )

        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(vars(parser.parse_args(command)), expected)

    def test_top_level_registrars_construct_independently(self):
        cases = (
            (add_runtime_commands, ["serve"], {"command": "serve"}),
            (add_database_commands, ["db", "health"], {"command": "db", "db_command": "health"}),
            (
                add_news_commands,
                ["news", "bus-policy", "verify"],
                {"command": "news", "news_command": "bus-policy", "policy_action": "verify"},
            ),
            (
                add_trading_commands,
                ["trading", "cases"],
                {"command": "trading", "trading_command": "cases", "state": None, "limit": 20},
            ),
            (
                add_ops_commands,
                ["ops", "validate-projections"],
                {"command": "ops", "ops_command": "validate-projections", "sample": 100},
            ),
        )

        for registrar, command, expected in cases:
            with self.subTest(registrar=registrar.__module__):
                parser = argparse.ArgumentParser(prog="tracefold")
                subcommands = parser.add_subparsers(dest="command")
                registrar(subcommands)
                self.assertEqual(vars(parser.parse_args(command)), expected)

    def test_audit_and_current_operations_commands_are_registered(self):
        parser = build_parser()

        commands = [
            ["db", "audit"],
            ["db", "query-audit"],
            ["db", "query-audit", "--analyze"],
            ["db", "audit", "--deep"],
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
        self.assertTrue(parsed[3].deep)
        self.assertEqual(parsed[4].ops_command, "validate-projections")
        self.assertEqual(parsed[4].sample, 5)
        self.assertEqual((parsed[5].news_command, parsed[5].review_command), ("review", "queue"))
        self.assertEqual((parsed[5].event, parsed[5].limit), ("ev-1", 5))
        self.assertEqual((parsed[6].news_command, parsed[6].dlq_action, parsed[6].limit), ("dlq", "inspect", 5))
        self.assertEqual((parsed[7].news_command, parsed[7].review_command), ("review", "evidence"))
        self.assertEqual((parsed[7].task, parsed[7].version), ("evt.ev-1.1.0123456789abcdef", "a" * 64))

    def test_retired_trading_authority_commands_are_rejected(self):
        parser = build_parser()
        for command in (
            ["trading", "authority", "risk-policy-install", "--file", "risk-policy.yaml"],
            ["trading", "authority", "activate", "--arm", "a" * 64],
            ["trading", "control", "running"],
            ["trading", "evidence", "verify", "--receipt", "a" * 64],
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(command)

    def test_trading_issue_does_not_accept_capital_or_order_parameters(self):
        parser = build_parser()
        for option in ("--quantity", "--notional", "--leverage", "--order-type", "--venue"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "trading",
                        "issue",
                        "/pause maintenance",
                        "--request-id",
                        "ops-1",
                        "--requested-at-ns",
                        "1788218708000000000",
                        option,
                        "1",
                    ]
                )

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
            ["trading", "refresh-capabilities"],
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
            write_runtime_config(
                home,
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
        self.assertEqual(
            news["push"],
            {
                "requested": False,
                "delivery_available": False,
                "reason": None,
                "provider": None,
                "feishu_webhook_url_configured": False,
                "feishu_signing_secret_configured": False,
                "telegram_bot_token_file_configured": False,
                "telegram_chat_id_configured": False,
                "min_interval_seconds": 0.6,
            },
        )
        self.assertNotIn("rss_enabled", news)
        self.assertNotIn("brief", news)
        self.assertNotIn("title_presentation", news)
        trading = payload["data"]["trading"]
        self.assertFalse(trading["enabled"])
        self.assertEqual(
            trading["execution"],
            {
                "mode": "disabled",
                "profile_id": "binance_usdm_primary",
                "account_slot": "binance_usdm_primary",
                "credentials": {
                    "api_key_file": str(home / ".tracefold" / "binance_usdm_api_key"),
                    "api_secret_file": str(home / ".tracefold" / "binance_usdm_api_secret"),
                },
            },
        )
        self.assertNotIn("private-strategy-alpha", stdout.getvalue())
        self.assertNotIn("private-strategy-beta", stdout.getvalue())
        self.assertEqual(payload["data"]["store"]["engine"], "postgresql")
        self.assertNotIn("postgres:postgres", payload["data"]["store"]["postgres"]["dsn"])
        self.assertIn("tracefold_test", payload["data"]["store"]["postgres"]["dsn"])
        self.assertIsNone(payload["data"]["store"]["postgres"]["password_file"])
        self.assertEqual(payload["data"]["store"]["serve_pool_max_size"], 7)
        self.assertEqual(payload["data"]["store"]["workers_pool_max_size"], 8)
        self.assertNotIn("embed" + "ding_dim", payload["data"]["store"])
        self.assertNotIn("workers", payload["data"])

    def test_generated_default_config_matches_the_current_hard_cut_schema(self):
        payload = yaml.safe_load(default_config_yaml())

        settings = Settings.model_validate(payload)

        self.assertEqual(
            settings.storage.postgres.dsn,
            "postgresql://tracefold@postgres:5432/tracefold",
        )
        self.assertEqual(settings.storage.postgres.password_file, "postgres_database_password")
        self.assertTrue(settings.news.enabled)
        self.assertNotIn("rss_enabled", payload["news"])
        self.assertNotIn("title_presentation", payload["news"])
        self.assertNotIn("news_brief_model", payload.get("llm") or {})
        self.assertEqual(
            payload["llm"]["news_reader_card"],
            {
                "api_key": None,
                "base_url": None,
                "model": None,
                "request": {
                    "send_temperature": None,
                    "temperature": 0,
                    "structured_output": "auto",
                    "extra_body": {},
                },
            },
        )
        self.assertEqual(
            payload["llm"]["news_reader_card_fallback"],
            {
                "api_key": None,
                "base_url": None,
                "model": None,
                "request": {
                    "send_temperature": None,
                    "temperature": 0,
                    "structured_output": "auto",
                    "extra_body": {},
                },
            },
        )
        self.assertNotIn("opennews_strategy_ids", payload["news"])
        self.assertEqual(payload["news"]["broker"]["url"], "amqp://tracefold:tracefold@rabbitmq:5672/")
        self.assertEqual(settings.news.broker.name_prefix, "")
        self.assertFalse(settings.news.push.enabled)
        self.assertEqual(
            payload["news"]["push"],
            {
                "enabled": False,
                "feishu_webhook_url": None,
                "feishu_signing_secret": None,
                "telegram_bot_token_file": None,
                "telegram_chat_id": None,
                "min_interval_seconds": 0.6,
            },
        )
        self.assertNotIn("providers", payload)
        self.assertNotIn("macro_document_analysis_enabled", payload["llm"])
        self.assertEqual(set(payload), {"ws_token", "api", "storage", "llm", "news", "trading"})
        self.assertEqual(
            payload["trading"],
            {
                "enabled": False,
                "control": {
                    "enabled": False,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_webhook_secret_file": "telegram_webhook_secret",
                    "allowed_chat_ids": [],
                    "allowed_user_ids": [],
                    "notification_chat_id": None,
                },
                "execution": {
                    "mode": "disabled",
                    "profile_id": "binance_usdm_primary",
                    "account_slot": "binance_usdm_primary",
                    "credentials": {
                        "api_key_file": "binance_usdm_api_key",
                        "api_secret_file": "binance_usdm_api_secret",
                    },
                },
            },
        )

    def test_config_reports_binding_credentials_without_disclosing_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            write_runtime_config(home)
            app_home = home / ".tracefold"
            key = "demo-key-value"
            secret = "demo-secret-value"
            for name, value in (("binance_usdm_api_key", key), ("binance_usdm_api_secret", secret)):
                path = app_home / name
                path.write_text(value, encoding="utf-8")
                path.chmod(0o600)
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                exit_code = main(["config"], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["data"]["trading"]["execution"]["credentials"],
            {
                "api_key_file": str(app_home / "binance_usdm_api_key"),
                "api_secret_file": str(app_home / "binance_usdm_api_secret"),
            },
        )
        self.assertNotIn(key, stdout.getvalue())
        self.assertNotIn(secret, stdout.getvalue())

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

    def test_nautilus_instrument_and_database_cadence_are_code_owned(self):
        for field, value in (
            ("instrument_id", "SOLUSDT-PERP.BINANCE"),
            ("poll_seconds", 1.0),
            ("accept_intents", True),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Settings.model_validate({"trading": {"nautilus": {field: value}}})

        with self.assertRaises(ValidationError):
            Settings.model_validate({"trading": {"poll_seconds": 1.0}})

    def test_news_replay_reports_offline_gate_counts_from_a_hits_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            write_runtime_config(home)
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
    assert 'dsn: "postgresql://tracefold@postgres:5432/tracefold"' in config_path.read_text(encoding="utf-8")
    for directory_name in ("logs", "cache"):
        directory = app_home / directory_name
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700
    for name in (
        "telegram_bot_token",
        "telegram_webhook_secret",
        "binance_usdm_api_key",
        "binance_usdm_api_secret",
        "postgres_password",
        "postgres_database_password",
    ):
        path = app_home / name
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600
    assert (app_home / "telegram_bot_token").read_bytes() == b""
    assert (app_home / "telegram_webhook_secret").read_bytes() == b""
    assert all((app_home / name).read_bytes() == b"" for name in ("binance_usdm_api_key", "binance_usdm_api_secret"))


def test_init_is_idempotent_and_does_not_rotate_operator_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first_stdout = io.StringIO()
    second_stdout = io.StringIO()

    assert main(["init"], stdout=first_stdout) == 0
    app_home = tmp_path / ".tracefold"
    tracked_names = (
        "config.yaml",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "binance_usdm_api_key",
        "binance_usdm_api_secret",
        "postgres_password",
        "postgres_database_password",
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


def test_init_migrates_the_pre_433c_trading_config_without_losing_operator_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app_home = tmp_path / ".tracefold"
    app_home.mkdir(parents=True)
    config_path = app_home / "config.yaml"
    old_payload = {
        "ws_token": "operator-owned-token",
        "storage": {"postgres": {"dsn": "postgresql://tracefold@postgres:5432/tracefold"}},
        "news": {"enabled": False},
        "trading": {
            "enabled": True,
            "candidates": {"max_age_seconds": 240, "min_oi_value_usd": 30_000_000},
            "order": {"fixed_notional_usd": 7},
            "bindings": {
                "binance_usdm": {
                    "api_key_file": "operator-binance-key",
                    "api_secret_file": "operator-binance-secret",
                },
                "hyperliquid_perp": {
                    "private_key_file": "operator-hyperliquid-key",
                    "account_address": "0xoperator",
                },
            },
        },
    }
    old_bytes = yaml.safe_dump(old_payload, sort_keys=False).encode()
    config_path.write_bytes(old_bytes)

    stdout = io.StringIO()
    assert main(["init"], stdout=stdout) == 0

    result = json.loads(stdout.getvalue())["data"]
    assert result["config_migrated"] is True
    backup_path = app_home / "config.pre-433c.yaml"
    assert result["config_backup_path"] == str(backup_path)
    assert backup_path.read_bytes() == old_bytes
    assert backup_path.stat().st_mode & 0o777 == 0o600
    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert migrated["ws_token"] == "operator-owned-token"
    assert migrated["news"] == {"enabled": False}
    assert migrated["trading"] == {
        "enabled": True,
        "candidates": {"max_age_seconds": 240, "min_oi_value_usd": 30_000_000},
        "execution": {
            "mode": "disabled",
            "profile_id": "binance_usdm_primary",
            "account_slot": "binance_usdm_primary",
            "credentials": {
                "api_key_file": "operator-binance-key",
                "api_secret_file": "operator-binance-secret",
            },
        },
    }
    Settings.model_validate(migrated)
    migrated_bytes = config_path.read_bytes()

    second_stdout = io.StringIO()
    assert main(["init"], stdout=second_stdout) == 0

    second_result = json.loads(second_stdout.getvalue())["data"]
    assert second_result["config_migrated"] is False
    assert second_result["config_backup_path"] is None
    assert config_path.read_bytes() == migrated_bytes
    assert backup_path.read_bytes() == old_bytes


def test_init_refuses_a_mixed_pre_and_post_433c_trading_config_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app_home = tmp_path / ".tracefold"
    app_home.mkdir(parents=True)
    config_path = app_home / "config.yaml"
    mixed_payload = {
        "trading": {
            "order": {"fixed_notional_usd": 10},
            "execution": {"mode": "disabled"},
        }
    }
    original = yaml.safe_dump(mixed_payload, sort_keys=False).encode()
    config_path.write_bytes(original)

    with pytest.raises(ValueError, match="trading_config_cutover_mixed_shape"):
        main(["init"], stdout=io.StringIO())

    assert config_path.read_bytes() == original
    assert not (app_home / "config.pre-433c.yaml").exists()


def test_init_refuses_a_conflicting_pre_433c_backup_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app_home = tmp_path / ".tracefold"
    app_home.mkdir(parents=True)
    config_path = app_home / "config.yaml"
    original = b"trading:\n  order:\n    fixed_notional_usd: 10\n"
    config_path.write_bytes(original)
    backup_path = app_home / "config.pre-433c.yaml"
    conflicting_backup = b"operator-owned-existing-backup\n"
    backup_path.write_bytes(conflicting_backup)

    with pytest.raises(ValueError, match="trading_config_cutover_backup_conflict"):
        main(["init"], stdout=io.StringIO())

    assert config_path.read_bytes() == original
    assert backup_path.read_bytes() == conflicting_backup


def test_init_refuses_a_config_symlink_without_touching_its_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app_home = tmp_path / ".tracefold"
    app_home.mkdir(parents=True)
    target = tmp_path / "operator-config-target.yaml"
    original = b"trading:\n  order:\n    fixed_notional_usd: 10\n"
    target.write_bytes(original)
    (app_home / "config.yaml").symlink_to(target)

    with pytest.raises(ValueError, match="config_path_not_regular_file"):
        main(["init"], stdout=io.StringIO())

    assert target.read_bytes() == original
    assert not (app_home / "config.pre-433c.yaml").exists()


if __name__ == "__main__":
    unittest.main()
