#!/bin/sh

# One-time cluster bootstrap for a pre-#112 PostgreSQL volume. Migrations run
# as tracefold_migrate and deliberately cannot create login roles; the retired
# bootstrap superuser is NOLOGIN. Run this script only through the Make target,
# with PostgreSQL stopped, so the official image opens the existing cluster in
# single-user mode without exposing a network listener.

set -eu

secret_dir=${1:-/run/secrets}
secret_path="${secret_dir}/postgres_review_password"

if [ "$(id -u)" -eq 0 ]; then
  echo "Review-role provisioning must run as the postgres OS user" >&2
  exit 1
fi
if [ -z "${PGDATA:-}" ] || [ ! -s "${PGDATA}/PG_VERSION" ]; then
  echo "Review-role provisioning requires an existing PGDATA" >&2
  exit 1
fi
if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  echo "PostgreSQL must be stopped before review-role provisioning" >&2
  exit 1
fi
if [ ! -r "$secret_path" ]; then
  echo "Review-role password file is not readable" >&2
  exit 1
fi

review_password=$(sed -n '1p' "$secret_path")
password_length=${#review_password}
file_size=$(wc -c < "$secret_path")

cleanup() {
  unset review_password
  [ -z "${role_sql:-}" ] || rm -f "$role_sql"
  [ -z "${role_output:-}" ] || rm -f "$role_output"
}
trap cleanup EXIT INT TERM

if [ "$file_size" -ne "$((password_length + 1))" ]; then
  echo "Review-role password must be one newline-terminated value" >&2
  exit 1
fi
if [ "$password_length" -lt 32 ] || [ "$password_length" -gt 128 ]; then
  echo "Review-role password length is invalid" >&2
  exit 1
fi
case "$review_password" in
  *[!A-Za-z0-9_-]*)
    echo "Review-role password charset is invalid" >&2
    exit 1
    ;;
esac

umask 077
role_sql=$(mktemp)
role_output=$(mktemp)
sed "s/__TRACEFOLD_REVIEW_PASSWORD__/${review_password}/g" > "$role_sql" <<'EOSQL'
DO $provision_review_role$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_review') THEN ALTER ROLE tracefold_review LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '__TRACEFOLD_REVIEW_PASSWORD__'; ELSE CREATE ROLE tracefold_review LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '__TRACEFOLD_REVIEW_PASSWORD__'; END IF; END $provision_review_role$;
EOSQL

if ! postgres --single -D "$PGDATA" -c log_statement=none "${POSTGRES_DB:-tracefold}" \
  < "$role_sql" > "$role_output" 2>&1; then
  echo "Review-role single-user provisioning failed" >&2
  exit 1
fi
if grep -Eq ' (ERROR|FATAL|PANIC):' "$role_output"; then
  echo "Review-role single-user provisioning reported a PostgreSQL error" >&2
  exit 1
fi
echo "tracefold_review role provisioned; start PostgreSQL and run the normal migration"
