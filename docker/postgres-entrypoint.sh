#!/bin/sh

set -eu

# Local Compose secrets keep the host file's 0600 mode. The official image
# drops to postgres before running initdb scripts, so copy this bootstrap-only
# secret into a private container path owned by that user first.
install -d -m 0700 -o postgres -g postgres /run/tracefold
install -m 0400 -o postgres -g postgres \
  /run/secrets/postgres_database_password \
  /run/tracefold/postgres_database_password
export TRACEFOLD_POSTGRES_SECRET_DIR=/run/tracefold

exec /usr/local/bin/docker-entrypoint.sh "$@"
