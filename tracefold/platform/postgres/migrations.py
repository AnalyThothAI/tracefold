from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def alembic_config() -> Config:
    # `alembic.ini` and the `versions/` tree are repository content, not package data: the flat
    # layout puts this module at `<root>/tracefold/platform/postgres/migrations.py`, so the
    # repository root is exactly three parents up. Migrations run from a checkout or from the
    # image's `/app`, never from a bare wheel install.
    root = Path(__file__).resolve().parents[3]
    return Config(str(root / "alembic.ini"))


def upgrade_head(database_url: str | None = None) -> None:
    config = alembic_config()
    if database_url is not None:
        config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def latest_migration_version() -> str:
    return str(ScriptDirectory.from_config(alembic_config()).get_current_head())
