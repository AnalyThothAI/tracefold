from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "tracefold/platform/postgres/alembic/script.py.mako"


def test_new_revision_template_requires_operational_evidence() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

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
        assert f"- {field}:" in template

    assert "use the verified backup-restore path" in template
    assert "TODO(exact major, family, and digest)" in template
