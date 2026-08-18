from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GMGN_PROVIDER_DRIFT") != "1",
    reason="set GMGN_PROVIDER_DRIFT=1 to run opt-in provider drift checks",
)


def test_live_provider_configuration_shape_matches_redacted_contract() -> None:
    from tracefold.app.cli.commands.config import handle_config
    from tracefold.platform.config.settings import load_settings

    try:
        settings = load_settings(require_ws_token=False)
    except FileNotFoundError as exc:
        pytest.skip(f"runtime config unavailable: {exc}")

    code, payload = handle_config(object())
    summary = {
        "config_path": str(settings.app_home / "config.yaml"),
        "providers": {
            "gmgn_configured": settings.gmgn_configured,
        },
    }

    mismatches: list[str] = []
    if code != 0:
        mismatches.append(f"config command returned code {code}")
    if not payload.get("ok"):
        mismatches.append("config command did not return ok=true")
    if Path(summary["config_path"]).parent != Path.home() / ".tracefold":
        mismatches.append("config_path is not under ~/.tracefold")
    mismatches.extend(_secret_leaks(payload.get("data", {})))

    if mismatches:
        pytest.fail(
            "Provider drift static capability/config shape mismatched.\n"
            f"Summary: {summary}\n"
            "Mismatches:\n- " + "\n- ".join(mismatches)
        )


def _secret_leaks(value: Any, *, path: str = "data") -> list[str]:
    leaks: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            child_path = f"{path}.{key}"
            secret_like_key = (
                "secret" in lowered
                or "passphrase" in lowered
                or lowered == "api_key"
                or lowered.endswith("_api_key")
                or lowered == "token"
                or lowered.endswith("_token")
            ) and not lowered.endswith("_configured")
            if secret_like_key and not isinstance(child, bool):
                leaks.append(f"{child_path} should be redacted to a boolean or omitted")
            leaks.extend(_secret_leaks(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            leaks.extend(_secret_leaks(child, path=f"{path}[{index}]"))
    return leaks
