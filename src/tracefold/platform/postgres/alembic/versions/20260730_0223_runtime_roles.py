"""Hard-cut PostgreSQL ownership and runtime permissions by composition role."""

from __future__ import annotations

from alembic import op

revision = "20260730_0223"
down_revision = "20260730_0222"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $roles$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_owner') THEN
            CREATE ROLE tracefold_owner NOLOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_serve') THEN
            CREATE ROLE tracefold_serve LOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_workers') THEN
            CREATE ROLE tracefold_workers LOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_migrate') THEN
            CREATE ROLE tracefold_migrate LOGIN NOINHERIT;
          END IF;
        END
        $roles$;

        ALTER ROLE tracefold_owner NOLOGIN;
        ALTER ROLE tracefold_serve LOGIN;
        ALTER ROLE tracefold_serve SET default_transaction_read_only = on;
        ALTER ROLE tracefold_workers LOGIN;
        ALTER ROLE tracefold_migrate LOGIN NOINHERIT;

        GRANT tracefold_owner TO tracefold_migrate;

        REVOKE CREATE ON SCHEMA public FROM PUBLIC;
        ALTER SCHEMA public OWNER TO tracefold_owner;

        DO $ownership$
        DECLARE
          object_row record;
        BEGIN
          FOR object_row IN
            SELECT c.relkind, n.nspname, c.relname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p', 'S', 'v', 'm')
              AND (
                c.relkind <> 'S'
                OR NOT EXISTS (
                  SELECT 1
                  FROM pg_depend AS dependency
                  WHERE dependency.classid = 'pg_class'::regclass
                    AND dependency.objid = c.oid
                    AND dependency.deptype IN ('a', 'i')
                )
              )
          LOOP
            IF object_row.relkind = 'S' THEN
              EXECUTE format(
                'ALTER SEQUENCE %I.%I OWNER TO tracefold_owner',
                object_row.nspname,
                object_row.relname
              );
            ELSIF object_row.relkind = 'v' THEN
              EXECUTE format(
                'ALTER VIEW %I.%I OWNER TO tracefold_owner',
                object_row.nspname,
                object_row.relname
              );
            ELSIF object_row.relkind = 'm' THEN
              EXECUTE format(
                'ALTER MATERIALIZED VIEW %I.%I OWNER TO tracefold_owner',
                object_row.nspname,
                object_row.relname
              );
            ELSE
              EXECUTE format(
                'ALTER TABLE %I.%I OWNER TO tracefold_owner',
                object_row.nspname,
                object_row.relname
              );
            END IF;
          END LOOP;
        END
        $ownership$;

        REVOKE ALL ON ALL TABLES IN SCHEMA public
          FROM tracefold_serve, tracefold_workers;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
          FROM tracefold_serve, tracefold_workers;

        GRANT USAGE ON SCHEMA public TO tracefold_serve, tracefold_workers;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE
          ON ALL TABLES IN SCHEMA public TO tracefold_workers;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tracefold_workers;

        ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public
          GRANT SELECT ON TABLES TO tracefold_serve;
        ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tracefold_workers;
        ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public
          GRANT USAGE, SELECT ON SEQUENCES TO tracefold_workers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0223 is an irreversible runtime-role hard cut")
