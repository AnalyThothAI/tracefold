"""Hard-cut PostgreSQL migrations to the direct object owner (#419).

Migration evidence:

- category: privilege
- why_database_must_change: the retired migrator role and owner membership are
  cluster-wide authority; the final database must attest their absence and the
  retained runtime ACL boundary at the durable authority
- current_source_revision: 20260831_0338
- minimum_supported_source_revision: 20260831_0338 after the offline role cut
- lock_level_and_order: role/catalog checks and ACL updates only; no table rewrite
- statement_timeout: 10s
- lock_timeout: 1s
- estimated_rows: 0
- estimated_bytes: 0
- rewrite_or_index_build: none
- preflight_and_maintenance_boundary: exact-main offline role cut followed by the
  existing stopped-writer migration gate
- archive_current_compatibility: no schema or data change
- role_and_grant_impact: tracefold_owner is the sole migration login; retained
  Serve, Workers, and Nautilus grants are restated without broadening them
- failure_state: transactional DDL rolls back and business processes remain stopped
- roll_forward_or_verified_backup_restore: roll forward only; downgrade is the
  verified pre-cut backup restore
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260831_0339
Revises: 20260831_0338
"""

from __future__ import annotations

from alembic import op

revision = "20260831_0339"
down_revision = "20260831_0338"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '1s'")
    op.execute("SET LOCAL statement_timeout = '10s'")
    op.execute(
        """
        DO $contract$
        BEGIN
          IF current_user <> 'tracefold_owner' THEN
            RAISE EXCEPTION 'tracefold_direct_owner_required';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_roles
             WHERE rolname = 'tracefold_owner'
               AND rolcanlogin AND rolinherit
               AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
               AND NOT rolreplication AND NOT rolbypassrls
          ) THEN
            RAISE EXCEPTION 'tracefold_owner_contract_invalid';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_roles
             WHERE rolname = 'tracefold_app' AND NOT rolcanlogin AND rolsuper
          ) THEN
            RAISE EXCEPTION 'tracefold_bootstrap_contract_invalid';
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_migrate') THEN
            RAISE EXCEPTION 'tracefold_migrate_role_present';
          END IF;
          IF EXISTS (
            SELECT 1
              FROM pg_auth_members membership
              JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
             WHERE granted_role.rolname = 'tracefold_owner'
          ) THEN
            RAISE EXCEPTION 'tracefold_runtime_owner_membership_present';
          END IF;
        END
        $contract$
        """
    )

    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("ALTER SCHEMA public OWNER TO tracefold_owner")
    op.execute("REVOKE ALL ON SCHEMA public FROM tracefold_serve, tracefold_workers, tracefold_nautilus")
    op.execute("GRANT USAGE ON SCHEMA public TO tracefold_serve, tracefold_workers, tracefold_nautilus")

    op.execute("REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM tracefold_serve")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO tracefold_serve")
    op.execute("GRANT INSERT ON news_reviews, news_external_miss_snapshots TO tracefold_serve")
    op.execute("REVOKE UPDATE, DELETE ON news_reviews, news_external_miss_snapshots FROM tracefold_serve")

    op.execute("REVOKE UPDATE, DELETE ON news_event_evidence_snapshots FROM tracefold_workers")
    op.execute("GRANT SELECT, INSERT ON news_event_evidence_snapshots TO tracefold_workers")
    op.execute("REVOKE UPDATE (execution_state) ON trading_intents FROM tracefold_workers")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON trading_nautilus_runtime_starts FROM tracefold_workers")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON trading_orders, trading_order_observations FROM tracefold_workers")

    op.execute("REVOKE INSERT, UPDATE, DELETE ON trading_cases FROM tracefold_nautilus")
    op.execute("REVOKE INSERT, DELETE ON trading_intents FROM tracefold_nautilus")
    op.execute("REVOKE UPDATE (case_id) ON trading_intents FROM tracefold_nautilus")
    op.execute("GRANT SELECT ON trading_intents TO tracefold_nautilus")
    op.execute("GRANT UPDATE (execution_state) ON trading_intents TO tracefold_nautilus")

    op.execute("REVOKE ALL ON FUNCTION purge_news_learning_retention(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION purge_news_learning_retention(integer) TO tracefold_workers")
    op.execute("REVOKE ALL ON FUNCTION materialize_trading_blacklist_expiry() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION materialize_trading_blacklist_expiry() TO tracefold_workers, tracefold_nautilus"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION store_trading_venue_catalog_snapshot("
        "TEXT, TEXT, BIGINT, BIGINT, INTEGER, JSONB, BIGINT) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION store_trading_venue_catalog_snapshot("
        "TEXT, TEXT, BIGINT, BIGINT, INTEGER, JSONB, BIGINT) TO tracefold_workers"
    )
    op.execute("REVOKE ALL ON FUNCTION trading_evidence_now_ms() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION trading_evidence_now_ms() TO tracefold_workers")
    op.execute("REVOKE ALL ON FUNCTION trading_canonical_jsonb(JSONB) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION trading_canonical_jsonb(JSONB) TO tracefold_workers, tracefold_nautilus")

    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public
          GRANT SELECT ON TABLES TO tracefold_serve
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tracefold_workers
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public
          GRANT USAGE, SELECT ON SEQUENCES TO tracefold_workers
        """
    )

    op.execute(
        """
        DO $ownership$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM pg_class object
              JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
              JOIN pg_roles owner ON owner.oid = object.relowner
             WHERE namespace.nspname = 'public'
               AND object.relkind IN ('r', 'p', 'S', 'v', 'm')
               AND owner.rolname <> 'tracefold_owner'
               AND NOT EXISTS (
                 SELECT 1 FROM pg_depend dependency
                  WHERE dependency.classid = 'pg_class'::regclass
                    AND dependency.objid = object.oid
                    AND dependency.deptype = 'e'
               )
          ) OR EXISTS (
            SELECT 1
              FROM pg_proc object
              JOIN pg_namespace namespace ON namespace.oid = object.pronamespace
              JOIN pg_roles owner ON owner.oid = object.proowner
             WHERE namespace.nspname = 'public'
               AND owner.rolname <> 'tracefold_owner'
               AND NOT EXISTS (
                 SELECT 1 FROM pg_depend dependency
                 WHERE dependency.classid = 'pg_proc'::regclass
                    AND dependency.objid = object.oid
                    AND dependency.deptype = 'e'
               )
          ) OR (SELECT count(*) FROM pg_default_acl) <> 2
          OR (
            SELECT count(*) FROM pg_default_acl defaults
            JOIN pg_roles owner ON owner.oid = defaults.defaclrole
            WHERE owner.rolname = 'tracefold_owner'
              AND defaults.defaclnamespace = 'public'::regnamespace
              AND defaults.defaclobjtype IN ('r', 'S')
          ) <> 2
          OR EXISTS (
            WITH actual(object_type, grantor_name, grantee_name, privilege_type, is_grantable) AS (
              SELECT defaults.defaclobjtype::text,
                     grantor.rolname::text,
                     COALESCE(grantee.rolname::text, 'PUBLIC'),
                     privilege.privilege_type,
                     privilege.is_grantable
              FROM pg_default_acl defaults
              CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privilege
              LEFT JOIN pg_roles grantor ON grantor.oid = privilege.grantor
              LEFT JOIN pg_roles grantee ON grantee.oid = privilege.grantee
            ),
            expected(object_type, grantor_name, grantee_name, privilege_type, is_grantable) AS (
              VALUES
                ('r', 'tracefold_owner', 'tracefold_serve', 'SELECT', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'DELETE', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'INSERT', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'SELECT', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'UPDATE', false),
                ('S', 'tracefold_owner', 'tracefold_workers', 'SELECT', false),
                ('S', 'tracefold_owner', 'tracefold_workers', 'USAGE', false)
            )
            SELECT 1
            FROM (
              (SELECT * FROM actual EXCEPT SELECT * FROM expected)
              UNION ALL
              (SELECT * FROM expected EXCEPT SELECT * FROM actual)
            ) mismatch
          )
          THEN
            RAISE EXCEPTION 'tracefold_application_object_owner_invalid';
          END IF;
          IF NOT has_table_privilege('tracefold_serve', 'news_events', 'SELECT')
             OR has_table_privilege('tracefold_serve', 'news_events', 'INSERT')
             OR NOT has_table_privilege('tracefold_serve', 'news_reviews', 'INSERT')
             OR has_schema_privilege('tracefold_workers', 'public', 'CREATE')
             OR NOT has_table_privilege(
               'tracefold_workers', 'news_event_evidence_snapshots', 'INSERT'
             )
             OR has_table_privilege(
               'tracefold_workers', 'news_event_evidence_snapshots', 'UPDATE, DELETE'
             )
             OR NOT has_column_privilege(
               'tracefold_workers', 'trading_intents', 'case_id', 'INSERT'
             )
             OR has_column_privilege(
               'tracefold_workers', 'trading_intents', 'execution_state', 'UPDATE'
             )
             OR has_table_privilege(
               'tracefold_workers', 'trading_nautilus_runtime_starts', 'INSERT, UPDATE, DELETE'
             )
             OR NOT has_column_privilege(
               'tracefold_nautilus', 'trading_intents', 'execution_state', 'UPDATE'
             )
             OR has_column_privilege(
               'tracefold_nautilus', 'trading_intents', 'case_id', 'UPDATE'
             )
             OR NOT has_function_privilege(
               'tracefold_workers', 'trading_evidence_now_ms()', 'EXECUTE'
             )
             OR NOT has_function_privilege(
               'tracefold_nautilus', 'trading_canonical_jsonb(JSONB)', 'EXECUTE'
             ) THEN
            RAISE EXCEPTION 'tracefold_runtime_acl_contract_invalid';
          END IF;
        END
        $ownership$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260831_0339 is an irreversible PostgreSQL role hard cut; restore the verified backup")
