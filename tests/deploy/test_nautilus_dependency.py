from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

pytestmark = pytest.mark.deploy

ROOT = Path(__file__).resolve().parents[2]
LINUX_WHEELS = {
    "x86_64": (
        "cp313-cp313-manylinux_2_35_x86_64",
        "429ea61c33a32cd8498d39e0ea95ebaa12b8dbfc25c71fbaba845f2b05e8ab91",
    ),
    "aarch64": (
        "cp313-cp313-manylinux_2_35_aarch64",
        "e536d7c925b3c475bef4f3f8e75196944f6b8758710e41da1109b8b837001690",
    ),
}


def test_public_trading_node_release_matches_the_locked_cp313_wheel() -> None:
    from nautilus_trader.live.node import TradingNode

    assert TradingNode.__module__ == "nautilus_trader.live.node"
    assert version("nautilus-trader") == "1.231.0"

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (package,) = (item for item in lock["package"] if item["name"] == "nautilus-trader")
    assert "nautilus_trader==1.231.0" in project["project"]["dependencies"]
    assert package["version"] == "1.231.0"
    for tag, sha256 in LINUX_WHEELS.values():
        wheel_name = f"nautilus_trader-1.231.0-{tag}.whl"
        (wheel,) = (item for item in package["wheels"] if item["url"].endswith(f"/{wheel_name}"))
        assert wheel["hash"] == f"sha256:{sha256}"


def test_python313_image_imports_the_public_trading_node_during_build() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert any(
        line.startswith("FROM python:3.13-slim-bookworm") and line.endswith(" AS python-deps")
        for line in dockerfile.splitlines()
    )
    assert "from nautilus_trader.live.node import TradingNode" in dockerfile
    assert "assert sys.version_info[:2] == (3, 13)" in dockerfile
    assert 'assert version("nautilus-trader") == "1.231.0"' in dockerfile
    assert "NAUTILUS_RELEASE" not in dockerfile
    assert "installed_nautilus_wheel_identity" not in dockerfile
