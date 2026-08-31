#!/bin/sh

set -eu

secret_dir=${1:-/run/secrets}
secret_path="${secret_dir}/postgres_migrate_password"
pgdata_path=${PGDATA:-/var/lib/postgresql/18/docker}
database_name=${POSTGRES_DB:-tracefold}

if [ -e "${pgdata_path}/postmaster.pid" ]; then
  echo "Tracefold PostgreSQL role hard cut is offline-only; stop the entire stack first" >&2
  exit 1
fi
if [ ! -r "$secret_path" ]; then
  echo "Tracefold PostgreSQL role password file is not readable: postgres_migrate_password" >&2
  exit 1
fi

owner_password=$(cat "$secret_path")
password_length=${#owner_password}
file_size=$(wc -c < "$secret_path")
trap 'unset owner_password' EXIT

if [ "$file_size" -ne "$((password_length + 1))" ]; then
  echo "Tracefold PostgreSQL role password file must contain one newline-terminated value: postgres_migrate_password" >&2
  exit 1
fi
if [ "$password_length" -lt 32 ] || [ "$password_length" -gt 128 ]; then
  echo "Tracefold PostgreSQL role password length is invalid: postgres_migrate_password" >&2
  exit 1
fi
case "$owner_password" in
  *[!A-Za-z0-9_-]*)
    echo "Tracefold PostgreSQL role password charset is invalid: postgres_migrate_password" >&2
    exit 1
    ;;
esac

postgres_single() {
  postgres --single -j -D "$pgdata_path" "$database_name"
}

final_contract() {
  contract_output=$(mktemp)
  postgres_single >"$contract_output" 2>&1 <<-'EOSQL' || true
	DO $contract$
	BEGIN
	  IF NOT EXISTS (
	    SELECT 1 FROM pg_roles
	     WHERE rolname = 'tracefold_owner'
	       AND rolcanlogin AND rolinherit
	       AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
	       AND NOT rolreplication AND NOT rolbypassrls
	  )
	  OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_migrate')
	  OR (SELECT count(*) FROM pg_roles WHERE rolname LIKE 'tracefold_%') <> 5
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_roles
	     WHERE rolname = 'tracefold_app'
	       AND NOT rolcanlogin AND rolinherit AND rolsuper
	       AND rolcreatedb AND rolcreaterole
	       AND NOT rolreplication AND NOT rolbypassrls
	  )
	  OR (
	    SELECT count(*) FROM pg_roles
	     WHERE rolname IN ('tracefold_serve', 'tracefold_workers', 'tracefold_nautilus')
	       AND rolcanlogin AND rolinherit
	       AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
	       AND NOT rolreplication AND NOT rolbypassrls
	  ) <> 3
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_roles
	     WHERE rolname = 'tracefold_serve'
	       AND COALESCE(rolconfig, ARRAY[]::text[])
	           @> ARRAY['default_transaction_read_only=on']
	  )
	  OR EXISTS (
	    SELECT 1
	      FROM pg_auth_members membership
	      JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
	      JOIN pg_roles member_role ON member_role.oid = membership.member
	     WHERE granted_role.rolname = 'tracefold_owner'
	        OR member_role.rolname = 'tracefold_owner'
	  )
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_namespace namespace
	    JOIN pg_roles owner ON owner.oid = namespace.nspowner
	    WHERE namespace.nspname = 'public' AND owner.rolname = 'tracefold_owner'
	  )
	  OR (SELECT count(*) FROM pg_default_acl) <> 2
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
	  OR (SELECT count(*) FROM alembic_version) <> 1
	  OR NOT EXISTS (
	    SELECT 1 FROM alembic_version
	     WHERE version_num IN ('20260831_0338', '20260831_0339')
	  )
	  OR NOT EXISTS (
	    SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED'
	  )
	  OR EXISTS (SELECT 1 FROM trading_cases WHERE state IN ('PENDING', 'RUNNING'))
	  OR EXISTS (
	    SELECT 1 FROM trading_intents
	     WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
	  )
	  OR EXISTS (
	    SELECT 1 FROM trading_orders
	     WHERE state IN (
	       'PREPARED', 'AWAITING_APPROVAL', 'APPROVED', 'SUBMITTING', 'AMBIGUOUS',
	       'RECONCILING', 'MANUAL_REVIEW_REQUIRED', 'ACKNOWLEDGED', 'PARTIAL',
	       'OPEN', 'UNPROTECTED', 'SAFETY_CLOSING'
	     )
	  ) THEN
	    RAISE EXCEPTION 'tracefold_role_hard_cut_final_contract_invalid';
	  END IF;
	  IF EXISTS (
	    SELECT 1 FROM pg_class object
	    JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
	    JOIN pg_roles owner ON owner.oid = object.relowner
	    WHERE namespace.nspname = 'public'
	      AND object.relkind IN ('r', 'p', 'S', 'v', 'm')
	      AND owner.rolname <> 'tracefold_owner'
	      AND NOT EXISTS (
	        SELECT 1 FROM pg_depend dependency
	         WHERE dependency.classid = 'pg_class'::regclass
	           AND dependency.objid = object.oid AND dependency.deptype = 'e'
	      )
	  ) OR EXISTS (
	    SELECT 1 FROM pg_proc object
	    JOIN pg_namespace namespace ON namespace.oid = object.pronamespace
	    JOIN pg_roles owner ON owner.oid = object.proowner
	    WHERE namespace.nspname = 'public'
	      AND owner.rolname <> 'tracefold_owner'
	      AND NOT EXISTS (
	        SELECT 1 FROM pg_depend dependency
	         WHERE dependency.classid = 'pg_proc'::regclass
	           AND dependency.objid = object.oid AND dependency.deptype = 'e'
	      )
	  ) THEN
	    RAISE EXCEPTION 'tracefold_role_hard_cut_object_owner_invalid';
	  END IF;
	  RAISE EXCEPTION '%', 'tracefold_role_hard_cut_final_' || 'contract_ok';
	END
	$contract$;
EOSQL

  if grep -q 'tracefold_role_hard_cut_final_contract_ok' "$contract_output"; then
    rm -f "$contract_output"
    return 0
  fi
  rm -f "$contract_output"
  return 1
}

old_contract() {
  contract_output=$(mktemp)
  postgres_single >"$contract_output" 2>&1 <<-'EOSQL' || true
	DO $contract$
	BEGIN
	  IF NOT EXISTS (
	    SELECT 1 FROM pg_roles
	     WHERE rolname = 'tracefold_owner'
	       AND NOT rolcanlogin AND rolinherit
	       AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
	       AND NOT rolreplication AND NOT rolbypassrls
	  )
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_roles
	     WHERE rolname = 'tracefold_migrate'
	       AND rolcanlogin AND NOT rolinherit
	       AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
	       AND NOT rolreplication AND NOT rolbypassrls
	  )
	  OR (SELECT count(*) FROM pg_roles WHERE rolname LIKE 'tracefold_%') <> 6
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_roles
	     WHERE rolname = 'tracefold_app'
	       AND NOT rolcanlogin AND rolinherit AND rolsuper
	       AND rolcreatedb AND rolcreaterole
	       AND NOT rolreplication AND NOT rolbypassrls
	  )
	  OR (
	    SELECT count(*) FROM pg_roles
	     WHERE rolname IN ('tracefold_serve', 'tracefold_workers', 'tracefold_nautilus')
	       AND rolcanlogin AND rolinherit
	       AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
	       AND NOT rolreplication AND NOT rolbypassrls
	  ) <> 3
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_roles
	     WHERE rolname = 'tracefold_serve'
	       AND COALESCE(rolconfig, ARRAY[]::text[])
	           @> ARRAY['default_transaction_read_only=on']
	  )
	  OR (
	    SELECT count(*) FROM pg_auth_members membership
	    JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
	    JOIN pg_roles member_role ON member_role.oid = membership.member
	    WHERE granted_role.rolname = 'tracefold_owner'
	       OR member_role.rolname = 'tracefold_owner'
	  ) <> 1
	  OR (
	    SELECT count(*) FROM pg_auth_members membership
	    JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
	    JOIN pg_roles member_role ON member_role.oid = membership.member
	    WHERE granted_role.rolname = 'tracefold_migrate'
	       OR member_role.rolname = 'tracefold_migrate'
	  ) <> 1
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_auth_members membership
	    JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
	    JOIN pg_roles member_role ON member_role.oid = membership.member
	    WHERE granted_role.rolname = 'tracefold_owner'
	      AND member_role.rolname = 'tracefold_migrate'
	      AND NOT membership.admin_option
	      AND NOT membership.inherit_option
	      AND membership.set_option
	  )
	  OR NOT EXISTS (
	    SELECT 1 FROM pg_namespace namespace
	    JOIN pg_roles owner ON owner.oid = namespace.nspowner
	    WHERE namespace.nspname = 'public' AND owner.rolname = 'tracefold_owner'
	  )
	  OR (SELECT count(*) FROM pg_default_acl) <> 2
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
	  OR (SELECT count(*) FROM alembic_version) <> 1
	  OR NOT EXISTS (
	    SELECT 1 FROM alembic_version WHERE version_num = '20260831_0338'
	  )
	  OR NOT EXISTS (
	    SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED'
	  )
	  OR EXISTS (SELECT 1 FROM trading_cases WHERE state IN ('PENDING', 'RUNNING'))
	  OR EXISTS (
	    SELECT 1 FROM trading_intents
	     WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
	  )
	  OR EXISTS (
	    SELECT 1 FROM trading_orders
	     WHERE state IN (
	       'PREPARED', 'AWAITING_APPROVAL', 'APPROVED', 'SUBMITTING', 'AMBIGUOUS',
	       'RECONCILING', 'MANUAL_REVIEW_REQUIRED', 'ACKNOWLEDGED', 'PARTIAL',
	       'OPEN', 'UNPROTECTED', 'SAFETY_CLOSING'
	     )
	  ) THEN
	    RAISE EXCEPTION 'tracefold_role_hard_cut_old_contract_invalid';
	  END IF;
	  IF EXISTS (
	    SELECT 1 FROM pg_class object
	    JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
	    JOIN pg_roles owner ON owner.oid = object.relowner
	    WHERE namespace.nspname = 'public'
	      AND object.relkind IN ('r', 'p', 'S', 'v', 'm')
	      AND owner.rolname <> 'tracefold_owner'
	      AND NOT EXISTS (
	        SELECT 1 FROM pg_depend dependency
	         WHERE dependency.classid = 'pg_class'::regclass
	           AND dependency.objid = object.oid AND dependency.deptype = 'e'
	      )
	  ) OR EXISTS (
	    SELECT 1 FROM pg_proc object
	    JOIN pg_namespace namespace ON namespace.oid = object.pronamespace
	    JOIN pg_roles owner ON owner.oid = object.proowner
	    WHERE namespace.nspname = 'public'
	      AND owner.rolname <> 'tracefold_owner'
	      AND NOT EXISTS (
	        SELECT 1 FROM pg_depend dependency
	         WHERE dependency.classid = 'pg_proc'::regclass
	           AND dependency.objid = object.oid AND dependency.deptype = 'e'
	      )
	  ) THEN
	    RAISE EXCEPTION 'tracefold_role_hard_cut_object_owner_invalid';
	  END IF;
	  RAISE EXCEPTION '%', 'tracefold_role_hard_cut_old_' || 'contract_ok';
	END
	$contract$;
EOSQL

  if grep -q 'tracefold_role_hard_cut_old_contract_ok' "$contract_output"; then
    rm -f "$contract_output"
    return 0
  fi
  rm -f "$contract_output"
  return 1
}

if final_contract; then
  echo "Tracefold PostgreSQL role hard cut already complete."
  exit 0
fi
if ! old_contract; then
  echo "Tracefold PostgreSQL role hard cut refused: database is neither the exact old nor final contract" >&2
  exit 1
fi

postgres_single >/dev/null 2>&1 <<-EOSQL || true
	BEGIN;
	ALTER ROLE tracefold_owner
	  LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
	  PASSWORD '${owner_password}';
	REVOKE tracefold_owner FROM tracefold_migrate;
	DROP ROLE tracefold_migrate;
	COMMIT;
EOSQL

if ! final_contract; then
  echo "Tracefold PostgreSQL role hard cut failed final verification; the transaction was rolled back" >&2
  exit 1
fi

echo "Tracefold PostgreSQL role hard cut complete: owner direct login enabled; retired migrator removed."
