from __future__ import annotations

from pathlib import Path

CONFIG_FILE_NAME = "config.yaml"


def app_home(path_override: str | Path | None = None) -> Path:
    if path_override:
        return Path(path_override).expanduser()
    return Path.home() / ".tracefold"


def app_log_path(app_home_override: str | Path | None = None) -> Path:
    return app_home(app_home_override) / "logs" / "tracefold.log"


def config_path(app_home_override: str | Path | None = None) -> Path:
    return app_home(app_home_override) / CONFIG_FILE_NAME
