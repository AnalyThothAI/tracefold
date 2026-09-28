from __future__ import annotations

from pathlib import Path

import yaml


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
