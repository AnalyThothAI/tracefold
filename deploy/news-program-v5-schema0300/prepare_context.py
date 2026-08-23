"""Materialize the pinned Program-v5 source plus the schema-0300 adapter."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

PROFILE_SCHEMA = "tracefold_news_rollback_image_profile_v1"
MIGRATION_NAME = "20260823_0300_trade_relevance_program_v6.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(*args: str, cwd: Path) -> bytes:
    completed = subprocess.run(  # noqa: S603 - every executable and argument shape is code-owned above
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _load_profile(bundle: Path) -> dict[str, Any]:
    resource = bundle / "profile.json"
    profile = json.loads(resource.read_text(encoding="utf-8"))
    canonical = json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n"
    if resource.read_text(encoding="utf-8") != canonical:
        raise ValueError("news_rollback_profile_not_canonical")
    if profile.get("schema_version") != PROFILE_SCHEMA:
        raise ValueError("news_rollback_profile_schema_invalid")
    return profile


def _extract_git_archive(repo: Path, revision: str, output: Path) -> None:
    archive = _run("git", "archive", "--format=tar", revision, cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("news_rollback_archive_path_invalid")
        bundle.extractall(output, filter="data")


def prepare(*, repo: Path, output: Path) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    output = output.resolve(strict=True)
    if any(output.iterdir()):
        raise ValueError("news_rollback_context_not_empty")
    bundle = Path(__file__).resolve().parent
    profile = _load_profile(bundle)
    revision = str(profile["source_revision"])

    _run("git", "cat-file", "-e", f"{revision}^{{commit}}", cwd=repo)
    _run("git", "merge-base", "--is-ancestor", revision, "HEAD", cwd=repo)

    adapter = bundle / "schema0300.patch"
    migration = repo / "src/tracefold/platform/postgres/alembic/versions" / MIGRATION_NAME
    if _sha256(adapter) != profile.get("adapter_patch_sha256"):
        raise ValueError("news_rollback_adapter_hash_mismatch")
    if _sha256(migration) != profile.get("migration_sha256"):
        raise ValueError("news_rollback_migration_hash_mismatch")

    _extract_git_archive(repo, revision, output)
    registry = output / "src/tracefold/news/agents/programs/registry.json"
    if _sha256(registry) != profile.get("registry_sha256"):
        raise ValueError("news_rollback_registry_hash_mismatch")

    _run("git", "apply", "--check", "--whitespace=error-all", str(adapter), cwd=output)
    _run("git", "apply", "--whitespace=error-all", str(adapter), cwd=output)

    migration_target = output / "src/tracefold/platform/postgres/alembic/versions" / MIGRATION_NAME
    shutil.copy2(migration, migration_target)
    verifier_target = output / "scripts/verify_news_rollback.py"
    shutil.copy2(bundle / "verify_runtime.py", verifier_target)
    drill_target = output / "scripts/drill_news_rollback.py"
    shutil.copy2(bundle / "drill_schema.py", drill_target)
    profile_target = output / "deploy/news-program-v5-schema0300/profile.json"
    profile_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle / "profile.json", profile_target)
    return profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = prepare(repo=args.repo, output=args.output)
    print(json.dumps(profile, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
