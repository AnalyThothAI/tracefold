#!/bin/sh

set -eu

secret_dir=${1:-/run/secrets}
secret_path="${secret_dir}/postgres_nautilus_password"
pgdata_path=${PGDATA:-/var/lib/postgresql/18/docker}
database_name=${POSTGRES_DB:-tracefold}

if [ -e "${pgdata_path}/postmaster.pid" ]; then
  echo "Tracefold Nautilus role provisioning is offline-only; stop PostgreSQL first" >&2
  exit 1
fi
if [ ! -r "$secret_path" ]; then
  echo "Tracefold PostgreSQL role password file is not readable: postgres_nautilus_password" >&2
  exit 1
fi

nautilus_password=$(cat "$secret_path")
password_length=${#nautilus_password}
file_size=$(wc -c < "$secret_path")
trap 'unset nautilus_password' EXIT

if [ "$file_size" -ne "$((password_length + 1))" ]; then
  echo "Tracefold PostgreSQL role password file must contain one newline-terminated value: postgres_nautilus_password" >&2
  exit 1
fi
if [ "$password_length" -lt 32 ] || [ "$password_length" -gt 128 ]; then
  echo "Tracefold PostgreSQL role password length is invalid: postgres_nautilus_password" >&2
  exit 1
fi
case "$nautilus_password" in
  *[!A-Za-z0-9_-]*)
    echo "Tracefold PostgreSQL role password charset is invalid: postgres_nautilus_password" >&2
    exit 1
    ;;
esac

# This is deliberately one role, not a general administrator surface. The command must run with
# PostgreSQL stopped and under the image's OS `postgres` user. Single-user mode supplies the local
# bootstrap authority without creating a network-login superuser or putting the password in argv;
# `-j` keeps the multiline SQL batch as one input unit.
if ! postgres --single -j -D "$pgdata_path" "$database_name" >/dev/null 2>&1 <<-EOSQL
	DO \$role\$
	BEGIN
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_nautilus') THEN
	    CREATE ROLE tracefold_nautilus LOGIN
	      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
	      PASSWORD '${nautilus_password}';
	  END IF;
	END
	\$role\$;
	ALTER ROLE tracefold_nautilus
	  LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
	  NOREPLICATION NOBYPASSRLS PASSWORD '${nautilus_password}';
EOSQL
then
  echo "Tracefold Nautilus role provisioning failed" >&2
  exit 1
fi
