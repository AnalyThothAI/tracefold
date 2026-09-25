from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.deploy

ROOT = Path(__file__).resolve().parents[2]
LINUX_WHEELS = {
    "cp313-cp313-manylinux_2_35_x86_64",
    "cp313-cp313-manylinux_2_35_aarch64",
}


def test_locked_nautilus_release_has_cp313_linux_wheels() -> None:
    from nautilus_trader.live.node import TradingNode

    assert TradingNode.__module__ == "nautilus_trader.live.node"

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (package,) = (item for item in lock["package"] if item["name"] == "nautilus-trader")
    assert any(dependency.startswith("nautilus_trader>=") for dependency in project["project"]["dependencies"])
    for tag in LINUX_WHEELS:
        wheel_name = f"nautilus_trader-{package['version']}-{tag}.whl"
        (wheel,) = (item for item in package["wheels"] if item["url"].endswith(f"/{wheel_name}"))
        assert wheel["hash"].startswith("sha256:")
        assert len(wheel["hash"]) == len("sha256:") + 64


def test_python313_image_imports_the_public_trading_node_during_build() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert any(
        line.startswith("FROM python:3.13-slim-bookworm") and line.endswith(" AS python-deps")
        for line in dockerfile.splitlines()
    )
    assert "from nautilus_trader.live.node import TradingNode" in dockerfile
    assert "assert sys.version_info[:2] == (3, 13)" in dockerfile
    assert 'assert version("nautilus-trader")' not in dockerfile
