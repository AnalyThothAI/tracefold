from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.client import local_docker_host_dsn, with_password_from_file

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    configured = config.attributes.get("database_url")
    if configured:
        return local_docker_host_dsn(str(configured))
    settings = load_settings(require_ws_token=False)
    return local_docker_host_dsn(
        with_password_from_file(
            settings.postgres_dsn("migrate"),
            settings.postgres_password_file("migrate"),
        )
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
    )
    with connectable.connect() as connection:
        owner_role_exists = bool(
            connection.exec_driver_sql(
                """
                SELECT CASE
                  WHEN current_user = 'tracefold_migrate'
                    AND EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_owner'
                  )
                  THEN pg_has_role(current_user, 'tracefold_owner', 'MEMBER')
                  ELSE false
                END
                """
            ).scalar()
        )
        if owner_role_exists:
            connection.exec_driver_sql("SET ROLE tracefold_owner")
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
