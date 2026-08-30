from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "tracefold/platform/postgres/alembic/script.py.mako"
GUIDE = ROOT / "docs/MIGRATIONS.md"
AUDITED_REVISIONS = (
    ROOT / "tracefold/platform/postgres/alembic/versions/20260830_0330_news_current_contract_hard_cut.py",
    ROOT / "tracefold/platform/postgres/alembic/versions/20260830_0331_trading_production_v3_contracts.py",
    ROOT / "tracefold/platform/postgres/alembic/versions/20260830_0332_trading_capital_authority_v1.py",
)


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


def test_0330_to_0332_authority_matrix_names_every_created_contract_object() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    patterns = (
        r"CREATE(?: OR REPLACE)? FUNCTION\s+([a-z0-9_]+)",
        r"CREATE TRIGGER\s+([a-z0-9_]+)",
        r"CREATE(?: OR REPLACE)? VIEW\s+([a-z0-9_]+)",
        r"CREATE(?: UNIQUE)? INDEX\s+([a-z0-9_]+)",
        r"(?<!DROP )CONSTRAINT\s+([a-z0-9_]+)",
    )
    created = {
        name
        for revision in AUDITED_REVISIONS
        for pattern in patterns
        for name in re.findall(pattern, revision.read_text(encoding="utf-8"), flags=re.IGNORECASE)
    }

    assert created
    assert sorted(name for name in created if f"`{name}`" not in guide) == []


def test_latest_current_event_view_revision_has_no_projection_wildcard() -> None:
    revision = (
        ROOT / "tracefold/platform/postgres/alembic/versions/20260830_0334_news_current_view_columns.py"
    ).read_text(encoding="utf-8")

    assert "SELECT *" not in revision
    assert "CREATE OR REPLACE VIEW news_current_events_v1" in revision
