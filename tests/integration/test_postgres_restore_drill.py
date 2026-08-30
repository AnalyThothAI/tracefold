from __future__ import annotations

import pytest

from tracefold.platform.postgres.restore_drill import POSTGRES_PRODUCTION_IMAGE, run_restore_drill

pytestmark = [pytest.mark.integration, pytest.mark.scheduled]


def test_production_image_dump_restore_migrate_audit_and_smoke(postgres_server_dsn: str) -> None:
    evidence = run_restore_drill(postgres_server_dsn)

    assert evidence["ok"] is True
    assert evidence["image_identity"] == POSTGRES_PRODUCTION_IMAGE
    assert evidence["source_head"] == evidence["restored_head"]
    assert all(evidence["smoke"].values())
    assert evidence["audit"] == {
        "mode": "deep",
        "migration_status": "ready",
        "news_schema_exact": True,
        "trading_schema_exact": True,
        "runtime_roles_ok": True,
    }
