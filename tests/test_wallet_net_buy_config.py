"""Offline config cutover preserves operator switches and never prints credentials."""

import json
import sys

import pytest
import yaml

from scripts.migrate_wallet_net_buy_config import main, migrate


def test_offline_migration_is_idempotent_and_preserves_unrelated_settings():
    source = {
        "ws_token": "private-token",
        "news": {
            "chain_tape": {
                "enabled": True,
                "notifications_enabled": False,
                "poll_interval_s": 7.5,
                "rules": {"buy_min_usd": 333, "crowding_n": 9, "trigger_max_age_s": 600},
                "digest": {"enabled": True, "interval_s": 14400},
            }
        },
    }
    result, removed = migrate(source, new_rules={})
    tape = result["news"]["chain_tape"]
    assert source["news"]["chain_tape"]["rules"]["buy_min_usd"] == 333
    assert tape["enabled"] and not tape["notifications_enabled"]
    assert tape["poll_interval_s"] == 7.5 and result["ws_token"] == "private-token"
    assert tape["rules"] == {"net_buy_fast_n": 3, "net_buy_slow_n": 5, "min_net_buy_usd": 1000, "trigger_max_age_s": 60}
    assert "digest" not in tape and removed
    assert migrate(result, new_rules={}) == (result, [])


def test_offline_cli_uses_separate_output_and_redacts_validation_errors(tmp_path, monkeypatch, capsys):
    source = tmp_path / "config.yaml"
    output = tmp_path / "next.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "ws_token": "keep-secret",
                "news": {
                    "chain_tape": {
                        "enabled": True,
                        "notifications_enabled": False,
                        "rules": {"buy_min_usd": 2000},
                    }
                },
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["migrate", "--config", str(source), "--output", str(output)])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "written"
    assert "buy_min_usd" in source.read_text()
    assert yaml.safe_load(output.read_text())["ws_token"] == "keep-secret"
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "unchanged"
    monkeypatch.setattr(sys, "argv", ["migrate", "--config", str(source), "--min-net-buy-usd", "invalid-secret"])
    assert main() == 2
    message = capsys.readouterr().out
    assert "invalid-secret" not in message and "keep-secret" not in message


@pytest.mark.parametrize(
    "source", [{"news": "secret"}, {"news": {"chain_tape": []}}, {"news": {"chain_tape": {"rules": "secret"}}}]
)
def test_invalid_hierarchy_is_refused_before_any_output(source):
    with pytest.raises(ValueError):
        migrate(source, new_rules={})
