from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.client import with_password_from_file
from tracefold.platform.postgres.maintenance_gate import MAINTENANCE_GATE_LOCK_KEYS

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    configured = config.attributes.get("database_url")
    if configured:
        return str(configured)
    settings = load_settings(require_ws_token=False)
    return with_password_from_file(
        settings.storage.postgres.dsn,
        settings.postgres_password_file(),
    )


def _sqlalchemy_database_url() -> str:
    url = _database_url()
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_sqlalchemy_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sqlalchemy_database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"application_name": "tracefold_migrate"},
    )
    with connectable.connect() as connection:
        if connection.exec_driver_sql("SELECT current_user").scalar() != "tracefold":
            raise RuntimeError("migration_owner_identity_required")
        connection.commit()
        acquired = bool(
            connection.exec_driver_sql(
                "SELECT pg_try_advisory_lock(%s, %s)",
                MAINTENANCE_GATE_LOCK_KEYS,
            ).scalar()
        )
        if not acquired:
            raise RuntimeError("steady_workers_runtime_active")
        connection.commit()
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.exec_driver_sql(
                "SELECT pg_advisory_unlock(%s, %s)",
                MAINTENANCE_GATE_LOCK_KEYS,
            )
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
