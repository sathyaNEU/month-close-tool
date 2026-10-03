#!/bin/sh
# The posting job's read-only role (Stage 7b, ADR-0017): close_ledger_reader may read the posting_queue view and
# nothing else (migration 0005 grants it). Idempotent: it runs at first init (docker-entrypoint-initdb.d) and again
# from the pg-roles step before every migration, so an existing data volume gets the role too.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v reader_password="$CLOSE_LEDGER_READER_PASSWORD" -v db="$POSTGRES_DB" <<'EOSQL'
SELECT format('CREATE ROLE close_ledger_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'close_ledger_reader') \gexec
ALTER ROLE close_ledger_reader LOGIN PASSWORD :'reader_password';
GRANT CONNECT ON DATABASE :"db" TO close_ledger_reader;
GRANT USAGE ON SCHEMA close TO close_ledger_reader;
ALTER ROLE close_ledger_reader SET search_path = close;
EOSQL
