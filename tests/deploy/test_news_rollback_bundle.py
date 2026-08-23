from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy/news-program-v5-schema0300"


def _profile() -> dict[str, object]:
    return json.loads((BUNDLE / "profile.json").read_text(encoding="utf-8"))


def test_rollback_profile_pins_reviewed_v5_and_schema_0300() -> None:
    profile = _profile()
    assert profile == {
        "adapter_patch_sha256": "8f0f1d91df151ddd7dbd5ddbdce712ec6cb8e1ac76772965a6c879c6bbee5dde",
        "factory_id": "tracefold.news.semantic_program.factory_v3",
        "learning_epoch": "program_v5",
        "migration_head": "20260823_0300",
        "migration_sha256": "82b81e1de52cced149240691bb2eb7149376cc6182a9daadd9769903fa5bce5f",
        "policy_version": "news_triage_policy_v9",
        "profile_id": "program_v5_schema0300_rollback",
        "program_sha256": "c62e0d69bf6c1901b3e8a1a716ca153acaf92793421d5af2701030c0477cac3b",
        "program_version": "news_semantic_program_v3",
        "registry_sha256": "07a40af24a081b480f75bab879239a36ebe690b19828905917ae6fea773f50d7",
        "schema_version": "tracefold_news_rollback_image_profile_v1",
        "source_revision": "7ab44ef00e539486954f6a73c6266dcd5d67dd4f",
    }
    assert hashlib.sha256((BUNDLE / "schema0300.patch").read_bytes()).hexdigest() == profile["adapter_patch_sha256"]


def test_prepare_context_builds_runnable_v5_with_only_schema_adapter(tmp_path: Path) -> None:
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
    runtime_registry = json.loads(
        (ROOT / "src/tracefold/news/agents/programs/registry.json").read_text(encoding="utf-8")
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
