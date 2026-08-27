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

    from tracefold.integrations.nautilus import NAUTILUS_LINUX_WHEELS, NAUTILUS_RELEASE

    assert TradingNode.__module__ == "nautilus_trader.live.node"
    assert NAUTILUS_RELEASE.version == "1.231.0"
    assert NAUTILUS_RELEASE.git_tag == "v1.231.0"
    assert NAUTILUS_RELEASE.git_commit == "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317"
    assert NAUTILUS_LINUX_WHEELS == LINUX_WHEELS
    assert version("nautilus-trader") == NAUTILUS_RELEASE.version

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (package,) = (item for item in lock["package"] if item["name"] == "nautilus-trader")
    assert "nautilus_trader==1.231.0" in project["project"]["dependencies"]
    assert package["version"] == NAUTILUS_RELEASE.version
    for tag, sha256 in LINUX_WHEELS.values():
        wheel_name = f"nautilus_trader-1.231.0-{tag}.whl"
        (wheel,) = (item for item in package["wheels"] if item["url"].endswith(f"/{wheel_name}"))
        assert wheel["hash"] == f"sha256:{sha256}"


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_linux_release_wheel_identity_is_architecture_exact(machine: str) -> None:
    from tracefold.integrations.nautilus.config import linux_release_wheel_identity

    tag, sha256 = LINUX_WHEELS[machine]
    assert linux_release_wheel_identity(machine) == f"{tag}@sha256:{sha256}"


def test_unknown_linux_architecture_has_no_release_identity() -> None:
    from tracefold.integrations.nautilus.config import linux_release_wheel_identity

    with pytest.raises(ValueError, match="nautilus_linux_wheel_architecture_unsupported"):
        linux_release_wheel_identity("riscv64")


def test_python313_image_imports_the_public_trading_node_during_build() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.13-slim-bookworm AS python-deps" in dockerfile
    assert "from nautilus_trader.live.node import TradingNode" in dockerfile
    assert "assert sys.version_info[:2] == (3, 13)" in dockerfile
    assert 'assert version("nautilus-trader") == NAUTILUS_RELEASE.version' in dockerfile
    assert "installed_nautilus_wheel_identity" in dockerfile
    assert "development@" in dockerfile
