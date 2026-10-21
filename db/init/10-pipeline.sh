#!/bin/sh
# Runs once, when the Postgres data directory is first created.
# Creates the pipeline database (if it differs from the n8n one) and its schema.
set -eu
DB="${PIPELINE_DB_NAME:-pipeline}"
if [ "$DB" != "$POSTGRES_DB" ]; then
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE \"$DB\""
fi
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$DB" -f /docker-entrypoint-initdb.d/schema.sql.inc
