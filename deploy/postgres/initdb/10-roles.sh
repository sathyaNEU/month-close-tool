#!/bin/sh
# Local Postgres init (runs once, on an empty data volume). Mirrors the production split:
#   close_migrator owns the schema and runs Alembic; close_app only reads and writes rows.
# Passwords come from .local/secrets/postgres.env (random, local development only).
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE ROLE close_migrator LOGIN PASSWORD '${CLOSE_MIGRATOR_PASSWORD}';
CREATE ROLE close_app LOGIN PASSWORD '${CLOSE_APP_PASSWORD}';
GRANT CONNECT, CREATE ON DATABASE ${POSTGRES_DB} TO close_migrator;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO close_app;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA close AUTHORIZATION close_migrator;
GRANT USAGE ON SCHEMA close TO close_app;
ALTER DEFAULT PRIVILEGES FOR ROLE close_migrator IN SCHEMA close
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO close_app;
ALTER DEFAULT PRIVILEGES FOR ROLE close_migrator IN SCHEMA close
  GRANT USAGE, SELECT ON SEQUENCES TO close_app;
ALTER ROLE close_migrator SET search_path = close;
ALTER ROLE close_app SET search_path = close;
EOSQL
