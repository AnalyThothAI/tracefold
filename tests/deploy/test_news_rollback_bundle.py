from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.deploy

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy/news-program-v5-schema0301"


def _profile() -> dict[str, object]:
    return json.loads((BUNDLE / "profile.json").read_text(encoding="utf-8"))


def test_rollback_profile_pins_reviewed_v5_and_schema_0301() -> None:
    profile = _profile()
    assert profile == {
        "adapter_patch_sha256": "574d7ea77cd57bc36a09270f9701323e79ac7ccaf5f10b6a039cd39957402f9f",
        "factory_id": "tracefold.news.semantic_program.factory_v3",
        "learning_epoch": "program_v5",
        "migration_head": "20260823_0301",
        "migration_sha256": "5184f2ecffce9dc205b563f89f8315dca0c2e275f6816a58f8a337f93e690222",
        "policy_version": "news_triage_policy_v9",
        "predecessor_migration_sha256": "d8e3d1d0733bc41f75e08615c94b68393cdc6a74ab574d6f81df55d2506c9d5e",
        "profile_id": "program_v5_schema0301_rollback",
        "program_sha256": "c62e0d69bf6c1901b3e8a1a716ca153acaf92793421d5af2701030c0477cac3b",
        "program_version": "news_semantic_program_v3",
        "registry_sha256": "07a40af24a081b480f75bab879239a36ebe690b19828905917ae6fea773f50d7",
        "schema_version": "tracefold_news_rollback_image_profile_v1",
        "source_revision": "66fc5dadcf44585fa4cd83f3c7495a62f32c047d",
    }
    assert hashlib.sha256((BUNDLE / "schema0301.patch").read_bytes()).hexdigest() == profile["adapter_patch_sha256"]


def test_prepare_context_builds_runnable_v5_with_schema_0301_adapter(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(BUNDLE / "prepare_context.py"),
            "--repo",
            str(ROOT),
            "--output",
            str(tmp_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert (tmp_path / "scripts/drill_news_rollback.py").is_file()
    versions = tmp_path / "src/tracefold/platform/postgres/alembic/versions"
    assert (versions / "20260823_0300_trading_core.py").is_file()
    assert (versions / "20260823_0301_trade_relevance_program_v6.py").is_file()
    repository = (tmp_path / "src/tracefold/news/repository.py").read_text(encoding="utf-8")
    trading_pipeline = (tmp_path / "src/tracefold/trading/pipeline.py").read_text(encoding="utf-8")
    assert repository.count("AND false -- News v5 rollback disables new Trading exposure.") == 2
    assert 'funnel.count("advance_reject:news_runtime_rollback")' in trading_pipeline
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(tmp_path / "src"),
    }
    subprocess.run(
        [sys.executable, str(tmp_path / "scripts/verify_news_rollback.py")],
        cwd=tmp_path,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )


def test_v6_image_has_no_rollback_registry_loader() -> None:
    profile = _profile()
    # The *current* registry, which #162 PR8-B moved to `news/program/resources`. The bundle's own
    # `prepare_context.py` keeps reading `news/agents/programs` on purpose: it reads that path out of the
    # pinned `source_revision` tree it extracts, where the Program still lived under `agents`.
    runtime_registry = json.loads(
        (ROOT / "src/tracefold/news/program/resources/registry.json").read_text(encoding="utf-8")
    )
    assert profile["program_sha256"] not in runtime_registry["images"]
    assert runtime_registry["stable"] != profile["program_sha256"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "TRACEFOLD_NEWS_PROGRAM_PROFILE" not in dockerfile
    assert "program_v3_rollback" not in dockerfile
    assert not (ROOT / "deploy/news-program-v3-rollback/profile.json").exists()
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert '"$$bundle/prepare_context.py"' in makefile
    assert '"$$bundle/drill_image.py"' in makefile
