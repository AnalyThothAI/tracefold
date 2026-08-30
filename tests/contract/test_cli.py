import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from tracefold.app.cli.parser import build_parser
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
        "storage": {
            "postgres": {
                "serve_dsn": postgres_dsn,
                "workers_dsn": postgres_dsn,
                "migrate_dsn": postgres_dsn,
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
        nautilus = parser.parse_args(["nautilus", "run"])
        assert (nautilus.command, nautilus.nautilus_command) == ("nautilus", "run")
        bootstrap = parser.parse_args(["nautilus", "run", "--bootstrap-zero-claims"])
        assert bootstrap.bootstrap_zero_claims is True

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

    def test_trading_authority_requires_explicit_artifacts_and_arm_set(self):
        parser = build_parser()

        policy = parser.parse_args(["trading", "authority", "risk-policy-install", "--file", "risk-policy.yaml"])
        activate = parser.parse_args(["trading", "authority", "activate", "--arm", "a" * 64, "--arm", "b" * 64])

        self.assertEqual((policy.trading_command, policy.authority_command), ("authority", "risk-policy-install"))
        self.assertEqual(policy.file, "risk-policy.yaml")
        self.assertEqual((activate.authority_command, activate.arm), ("activate", ["a" * 64, "b" * 64]))
        with self.assertRaises(SystemExit):
            parser.parse_args(["trading", "control", "running"])

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
        self.assertEqual(news["models"]["triage_fallback_models"], [])
        self.assertEqual(news["models"]["reader_card_fallback_models"], [])
        self.assertEqual(news["models"]["reader_card_fallback_dedicated"], [])
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
                "telegram_target_count": 0,
                "telegram_private_target_count": 0,
                "min_interval_seconds": 0.6,
            },
        )
        self.assertNotIn("rss_enabled", news)
        self.assertNotIn("brief", news)
        self.assertNotIn("title_presentation", news)
        trading = payload["data"]["trading"]
        self.assertFalse(trading["enabled"])
        self.assertEqual(trading["target_notional_usd"], "10")
        self.assertEqual(
            trading["bindings"],
            {
                "BINANCE_USDM": {
                    "credential_state": "unconfigured",
                    "api_key_file": str(home / ".tracefold" / "binance_usdm_api_key"),
                    "api_secret_file": str(home / ".tracefold" / "binance_usdm_api_secret"),
                },
                "HYPERLIQUID_PERP": {
                    "credential_state": "unconfigured",
                    "private_key_file": str(home / ".tracefold" / "hyperliquid_private_key"),
                    "account_address_configured": False,
                },
            },
        )
        self.assertNotIn("private-strategy-alpha", stdout.getvalue())
        self.assertNotIn("private-strategy-beta", stdout.getvalue())
        self.assertEqual(payload["data"]["store"]["engine"], "postgresql")
        self.assertEqual(
            set(payload["data"]["store"]["postgres_roles"]),
            {"serve", "workers", "migrate", "nautilus", "onchain"},
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
        self.assertEqual(payload["llm"]["news_fallbacks"], [])
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
                "telegram_chat_ids": [],
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
                "order": {"fixed_notional_usd": 10},
                "bindings": {
                    "binance_usdm": {
                        "api_key_file": "binance_usdm_api_key",
                        "api_secret_file": "binance_usdm_api_secret",
                    },
                    "hyperliquid_perp": {
                        "private_key_file": "hyperliquid_private_key",
                        "account_address": None,
                    },
                },
                "manual": {
                    "risk": {
                        "notional_deviation_limit_bps": 5000,
                        "tight_stop_deviation_limit_bps": 5000,
                        "wide_stop_deviation_limit_bps": 10000,
                        "max_account_risk_bps": 1000,
                        "high_risk_loss_multiple_bps": 15000,
                        "min_leverage": 1,
                        "max_leverage": 20,
                    },
                    "tight_stop": {
                        "leverage": 10,
                        "stop_loss_bps": 100,
                        "take_profit_bps": 200,
                        "account_risk_bps": 200,
                        "min_notional_usd": 5,
                        "max_notional_usd": 10,
                    },
                    "wide_stop": {
                        "leverage": 2,
                        "stop_loss_bps": 2000,
                        "take_profit_bps": 10000,
                        "account_risk_bps": 100,
                        "min_notional_usd": 5,
                        "max_notional_usd": 10,
                    },
                },
                "onchain": {
                    "slippage_bps": 100,
                    "discovery_chain_ids": [1, 56, 8453, 42161, 4663],
                    "settlement_assets": [
                        {
                            "chain_id": 1,
                            "chain_name": "Ethereum",
                            "symbol": "USDC",
                            "contract_address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                            "decimals": 6,
                            "quote_amount": 10,
                            "rpc_url": None,
                        },
                        {
                            "chain_id": 56,
                            "chain_name": "BNB Chain",
                            "symbol": "USDT",
                            "contract_address": "0x55d398326f99059ff775485246999027b3197955",
                            "decimals": 18,
                            "quote_amount": 10,
                            "rpc_url": None,
                        },
                        {
                            "chain_id": 8453,
                            "chain_name": "Base",
                            "symbol": "USDC",
                            "contract_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                            "decimals": 6,
                            "quote_amount": 10,
                            "rpc_url": None,
                        },
                        {
                            "chain_id": 42161,
                            "chain_name": "Arbitrum One",
                            "symbol": "USDC",
                            "contract_address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                            "decimals": 6,
                            "quote_amount": 10,
                            "rpc_url": None,
                        },
                        {
                            "chain_id": 4663,
                            "chain_name": "Robinhood Chain",
                            "symbol": "USDG",
                            "contract_address": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
                            "decimals": 6,
                            "quote_amount": 10,
                            "rpc_url": None,
                        },
                    ],
                },
                "telegram_profiles": [],
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
            payload["data"]["trading"]["bindings"]["BINANCE_USDM"]["credential_state"],
            "configured",
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
    for directory_name in ("logs", "cache"):
        directory = app_home / directory_name
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700
    for name in (
        "telegram_bot_token",
        "binance_usdm_api_key",
        "binance_usdm_api_secret",
        "hyperliquid_private_key",
        "postgres_password",
        "postgres_serve_password",
        "postgres_workers_password",
        "postgres_migrate_password",
        "postgres_nautilus_password",
        "postgres_onchain_password",
    ):
        path = app_home / name
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600
    assert (app_home / "telegram_bot_token").read_bytes() == b""
    assert all(
        (app_home / name).read_bytes() == b""
        for name in (
            "binance_usdm_api_key",
            "binance_usdm_api_secret",
            "hyperliquid_private_key",
        )
    )
    for directory in (
        app_home / "trading_profiles" / "manual",
        app_home / "trading_profiles" / "quotes",
        app_home / "trading_profiles" / "onchain",
    ):
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700


def test_init_is_idempotent_and_does_not_rotate_operator_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first_stdout = io.StringIO()
    second_stdout = io.StringIO()

    assert main(["init"], stdout=first_stdout) == 0
    app_home = tmp_path / ".tracefold"
    tracked_names = (
        "config.yaml",
        "telegram_bot_token",
        "binance_usdm_api_key",
        "binance_usdm_api_secret",
        "hyperliquid_private_key",
        "postgres_password",
        "postgres_serve_password",
        "postgres_workers_password",
        "postgres_migrate_password",
        "postgres_nautilus_password",
        "postgres_onchain_password",
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
