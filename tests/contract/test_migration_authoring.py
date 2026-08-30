from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "tracefold/platform/postgres/alembic/script.py.mako"


def test_new_revision_generation_requires_operational_evidence(tmp_path: Path) -> None:
    script_root = tmp_path / "alembic"
    versions = script_root / "versions"
    versions.mkdir(parents=True)
    shutil.copyfile(TEMPLATE, script_root / "script.py.mako")
    config = Config()
    config.set_main_option("script_location", str(script_root))
    generated = ScriptDirectory.from_config(config).generate_revision(
        "test_revision",
        "contract evidence",
        head=None,
        refresh=True,
    )
    revision = Path(generated.path).read_text(encoding="utf-8")

    for field in (
        "category",
        "why_database_must_change",
        "current_source_revision",
        "minimum_supported_source_revision",
        "lock_level_and_order",
        "statement_timeout",
        "lock_timeout",
        "estimated_rows",
        "estimated_bytes",
        "rewrite_or_index_build",
        "preflight_and_maintenance_boundary",
        "archive_current_compatibility",
        "role_and_grant_impact",
        "failure_state",
        "roll_forward_or_verified_backup_restore",
        "production_postgres_image",
    ):
        assert f"- {field}:" in revision

    assert "use the verified backup-restore path" in revision
    assert "TODO(exact major, family, and digest)" in revision
