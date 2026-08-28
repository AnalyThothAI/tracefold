from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tracefold.platform.postgres.legacy_reconciliation import reconcile_colliding_telegram_lineage


def alembic_config() -> Config:
    root = Path(__file__).resolve().parents[4]
    return Config(str(root / "alembic.ini"))


def upgrade_head(database_url: str | None = None) -> None:
    if database_url is not None:
        reconcile_colliding_telegram_lineage(database_url)
    config = alembic_config()
    if database_url is not None:
        config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def latest_migration_version() -> str:
    return str(ScriptDirectory.from_config(alembic_config()).get_current_head())
