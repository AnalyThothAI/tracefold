#!/bin/sh

set -eu

secret_dir=${1:-/run/secrets}
secret_path="${secret_dir}/postgres_onchain_password"
pgdata_path=${PGDATA:-/var/lib/postgresql/18/docker}
database_name=${POSTGRES_DB:-tracefold}

if [ -e "${pgdata_path}/postmaster.pid" ]; then
  echo "Tracefold onchain role provisioning is offline-only; stop PostgreSQL first" >&2
  exit 1
fi
if [ ! -r "$secret_path" ]; then
  echo "Tracefold PostgreSQL role password file is not readable: postgres_onchain_password" >&2
  exit 1
fi

onchain_password=$(cat "$secret_path")
password_length=${#onchain_password}
file_size=$(wc -c < "$secret_path")
trap 'unset onchain_password' EXIT

if [ "$file_size" -ne "$((password_length + 1))" ]; then
  echo "Tracefold PostgreSQL role password file must contain one newline-terminated value: postgres_onchain_password" >&2
  exit 1
fi
if [ "$password_length" -lt 32 ] || [ "$password_length" -gt 128 ]; then
  echo "Tracefold PostgreSQL role password length is invalid: postgres_onchain_password" >&2
  exit 1
fi
case "$onchain_password" in
  *[!A-Za-z0-9_-]*)
    echo "Tracefold PostgreSQL role password charset is invalid: postgres_onchain_password" >&2
    exit 1
    ;;
esac

# Single-user mode adds exactly the isolated onchain login to a stopped legacy
# volume before migration 0337 references it. No network superuser is created.
if ! postgres --single -j -D "$pgdata_path" "$database_name" >/dev/null 2>&1 <<-EOSQL
	DO \$role\$
	BEGIN
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_onchain') THEN
	    CREATE ROLE tracefold_onchain LOGIN
	      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
	      PASSWORD '${onchain_password}';
	  END IF;
	END
	\$role\$;
	ALTER ROLE tracefold_onchain
	  LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
	  NOREPLICATION NOBYPASSRLS PASSWORD '${onchain_password}';
EOSQL
then
  echo "Tracefold onchain role provisioning failed" >&2
  exit 1
fi
