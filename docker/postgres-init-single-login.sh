#!/bin/sh

set -eu

secret_dir=${1:-/run/secrets}

read_role_password() {
  secret_name=$1
  secret_path="${secret_dir}/${secret_name}"

  if [ ! -r "$secret_path" ]; then
    echo "Tracefold PostgreSQL role password file is not readable: ${secret_name}" >&2
    return 1
  fi

  password=$(cat "$secret_path")
  password_length=${#password}
  file_size=$(wc -c < "$secret_path")

  if [ "$file_size" -ne "$((password_length + 1))" ]; then
    echo "Tracefold PostgreSQL role password file must contain one newline-terminated value: ${secret_name}" >&2
    return 1
  fi
  if [ "$password_length" -lt 32 ] || [ "$password_length" -gt 128 ]; then
    echo "Tracefold PostgreSQL role password length is invalid: ${secret_name}" >&2
    return 1
  fi
  case "$password" in
    *[!A-Za-z0-9_-]*)
      echo "Tracefold PostgreSQL role password charset is invalid: ${secret_name}" >&2
      return 1
      ;;
  esac

  printf '%s' "$password"
}

database_password=$(read_role_password postgres_database_password)
trap 'unset database_password' EXIT

if [ "${POSTGRES_USER:-}" != "tracefold_app" ] || [ "${POSTGRES_DB:-}" != "tracefold" ]; then
  echo "Tracefold PostgreSQL bootstrap identity is invalid" >&2
  exit 1
fi

# The official image runs this file only while initializing an empty PGDATA.
# Install the non-trusted extension before revoking the bootstrap superuser's
# login, then hand all application DDL and data access to one login role.
psql --quiet --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	BEGIN;

	CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;
	CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

	CREATE ROLE tracefold
	  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
	  PASSWORD '${database_password}';

	REVOKE CREATE ON SCHEMA public FROM PUBLIC;
	ALTER SCHEMA public OWNER TO tracefold;
	ALTER ROLE tracefold_app NOLOGIN;

	COMMIT;
EOSQL
