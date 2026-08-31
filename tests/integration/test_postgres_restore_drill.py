from __future__ import annotations

import pytest

from tests.postgres_test_utils import postgres_migration_test_dsn, prepare_test_migration_database
from tracefold.app.restore_storage import run_restore_drill
from tracefold.platform.postgres.restore_drill import POSTGRES_PRODUCTION_IMAGE

pytestmark = [pytest.mark.integration, pytest.mark.scheduled]


def test_production_image_dump_restore_migrate_audit_and_smoke(postgres_server_dsn: str) -> None:
    prepare_test_migration_database(postgres_server_dsn)
    evidence = run_restore_drill(postgres_server_dsn, postgres_migration_test_dsn(postgres_server_dsn))

    assert evidence["ok"] is True
    assert evidence["image_identity"] == POSTGRES_PRODUCTION_IMAGE
    assert evidence["source_head"] == evidence["restored_head"]
    assert all(evidence["smoke"].values())
    assert evidence["smoke"]["trading_case_fact"] is True
    assert evidence["smoke"]["trading_signal_fact"] is True
    assert evidence["audit"] == {
        "mode": "deep",
        "migration_status": "ready",
        "news_schema_exact": True,
        "trading_schema_exact": True,
    }
