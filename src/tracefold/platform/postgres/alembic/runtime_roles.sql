DO $roles$
BEGIN
  IF current_user <> 'tracefold_owner' THEN
    IF NOT COALESCE(
      (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
      false
    ) THEN
      RAISE EXCEPTION 'tracefold_runtime_role_bootstrap_superuser_required';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_owner') THEN
      CREATE ROLE tracefold_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_serve') THEN
      CREATE ROLE tracefold_serve LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_workers') THEN
      CREATE ROLE tracefold_workers LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_review') THEN
      CREATE ROLE tracefold_review LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_migrate') THEN
      CREATE ROLE tracefold_migrate LOGIN NOINHERIT;
    END IF;

    ALTER ROLE tracefold_owner
      NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
    ALTER ROLE tracefold_serve
      LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
    ALTER ROLE tracefold_serve SET default_transaction_read_only = on;
    ALTER ROLE tracefold_workers
      LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
    ALTER ROLE tracefold_review
      LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
    ALTER ROLE tracefold_migrate
      LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;

    GRANT tracefold_owner TO tracefold_migrate WITH ADMIN FALSE;
    GRANT tracefold_owner TO tracefold_migrate WITH INHERIT FALSE;
    GRANT tracefold_owner TO tracefold_migrate WITH SET TRUE;
  END IF;
END
$roles$;

DO $role_contract$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_roles
     WHERE rolname = 'tracefold_owner'
       AND NOT rolcanlogin
       AND rolinherit
       AND NOT rolsuper
       AND NOT rolcreatedb
       AND NOT rolcreaterole
       AND NOT rolreplication
       AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:tracefold_owner';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_roles
     WHERE rolname = 'tracefold_serve'
       AND rolcanlogin
       AND rolinherit
       AND NOT rolsuper
       AND NOT rolcreatedb
       AND NOT rolcreaterole
       AND NOT rolreplication
       AND NOT rolbypassrls
       AND COALESCE(rolconfig, ARRAY[]::text[])
           @> ARRAY['default_transaction_read_only=on']
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:tracefold_serve';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_roles
     WHERE rolname = 'tracefold_workers'
       AND rolcanlogin
       AND rolinherit
       AND NOT rolsuper
       AND NOT rolcreatedb
       AND NOT rolcreaterole
       AND NOT rolreplication
       AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:tracefold_workers';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_roles
     WHERE rolname = 'tracefold_review'
       AND rolcanlogin
       AND rolinherit
       AND NOT rolsuper
       AND NOT rolcreatedb
       AND NOT rolcreaterole
       AND NOT rolreplication
       AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:tracefold_review';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_roles
     WHERE rolname = 'tracefold_migrate'
       AND rolcanlogin
       AND NOT rolinherit
       AND NOT rolsuper
       AND NOT rolcreatedb
       AND NOT rolcreaterole
       AND NOT rolreplication
       AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:tracefold_migrate';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_auth_members membership
      JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
      JOIN pg_roles member_role ON member_role.oid = membership.member
     WHERE granted_role.rolname = 'tracefold_owner'
       AND member_role.rolname = 'tracefold_migrate'
       AND NOT membership.admin_option
       AND NOT membership.inherit_option
       AND membership.set_option
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:tracefold_migrate_owner_membership';
  END IF;
  IF current_user = 'tracefold_owner' AND NOT EXISTS (
    SELECT 1
      FROM pg_namespace namespace
      JOIN pg_roles owner_role ON owner_role.oid = namespace.nspowner
     WHERE namespace.nspname = 'public'
       AND owner_role.rolname = 'tracefold_owner'
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:public_schema_owner';
  END IF;
  IF current_user = 'tracefold_owner' AND NOT EXISTS (
    SELECT 1
      FROM pg_roles
     WHERE rolname = 'tracefold_app'
       AND NOT rolcanlogin
  ) THEN
    RAISE EXCEPTION 'tracefold_runtime_role_contract_invalid:bootstrap_login_disabled';
  END IF;
END
$role_contract$;

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
  FROM tracefold_serve, tracefold_workers, tracefold_review;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
  FROM tracefold_serve, tracefold_workers, tracefold_review;

GRANT USAGE ON SCHEMA public TO tracefold_serve, tracefold_workers, tracefold_review;
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
