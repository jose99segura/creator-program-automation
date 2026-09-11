#!/usr/bin/env bash
#
# Put the workflows and their schema onto the VPS.
#
# Run it ON the box, over ssh, so the production password never leaves it:
#
#   scp -r n8n root@51.195.223.171:/tmp/creator-n8n
#   ssh root@51.195.223.171
#   CREATOR_DB_PASSWORD='...' bash /tmp/creator-n8n/deploy.sh
#
# Idempotent on purpose. The role and database are created only if absent, the
# schema is CREATE TABLE IF NOT EXISTS throughout, the golden set is an upsert,
# and the workflows have fixed ids so importing twice updates rather than
# duplicates. Running it again after a partial failure is the normal way to
# use it, which is the only kind of deploy script anyone actually trusts.
#
# What it does NOT do: create the n8n Postgres credential. n8n encrypts
# credentials with the instance key, so scripting that means writing the
# password somewhere in plaintext first. Create it once in the UI.

set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-postgres-shared}"
N8N_CONTAINER="${N8N_CONTAINER:-n8n}"
DB_NAME="${DB_NAME:-creator_prod}"
DB_ROLE="${DB_ROLE:-creator_prod}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Fail loudly on a missing secret rather than booting into a broken state:
# without this the psql below would create a role with an empty password and
# report success.
: "${CREATOR_DB_PASSWORD:?set CREATOR_DB_PASSWORD to the password for the ${DB_ROLE} role}"

say() { printf '\n=== %s\n' "$*"; }

require_container() {
  docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true \
    || { echo "container '$1' is not running" >&2; exit 1; }
}

say "checking containers"
require_container "$PG_CONTAINER"
require_container "$N8N_CONTAINER"
echo "ok: $PG_CONTAINER, $N8N_CONTAINER"

psql_super() { docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres "$@"; }

say "role and database"
# DO blocks rather than CREATE ... IF NOT EXISTS because Postgres has no such
# form for roles, and a bare CREATE ROLE aborts the whole script on a re-run.
psql_super -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DB_ROLE}') THEN
    CREATE ROLE ${DB_ROLE} LOGIN PASSWORD '${CREATOR_DB_PASSWORD}';
    RAISE NOTICE 'created role ${DB_ROLE}';
  ELSE
    RAISE NOTICE 'role ${DB_ROLE} already exists, password untouched';
  END IF;
END
\$\$;
SQL

if ! psql_super -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
  # OWNER, not GRANT. Migrations need table ownership: ALTER TABLE is refused
  # for anything but the owner, GRANT ALL does not help, and drizzle-style
  # tools exit 1 having printed nothing useful.
  psql_super -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_ROLE};"
  echo "created database ${DB_NAME}"
else
  echo "database ${DB_NAME} already exists"
fi

say "schema and golden set"
# Applied AS the app role, so every table it creates is its own.
for f in 01-schema.sql 02-golden-set.sql; do
  echo "-- $f"
  PGPASSWORD="$CREATOR_DB_PASSWORD" docker exec -i \
    -e PGPASSWORD="$CREATOR_DB_PASSWORD" "$PG_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -U "$DB_ROLE" -d "$DB_NAME" < "$HERE/sql/$f"
done

say "ownership check"
# A query, not a memory. Anything listed here was created by the wrong role
# and will refuse the next ALTER TABLE.
psql_super -d "$DB_NAME" -c \
  "SELECT tablename, tableowner FROM pg_tables
    WHERE schemaname = 'public' AND tableowner <> '${DB_ROLE}';"

say "grants check"
# ALTER DEFAULT PRIVILEGES only applies to objects created AFTER it runs, so
# 'the GRANT ran' proves nothing about tables a later migration added. This
# compares object by object: anything listed is invisible to the app role --
# not empty, not wrong, invisible.
psql_super -d "$DB_NAME" -c \
  "SELECT t.table_name
     FROM information_schema.tables t
     WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
       AND NOT EXISTS (
         SELECT 1 FROM information_schema.role_table_grants g
          WHERE g.table_schema = 'public'
            AND g.table_name = t.table_name
            AND g.grantee = '${DB_ROLE}'
            AND g.privilege_type = 'SELECT');"

say "importing workflows"
docker exec "$N8N_CONTAINER" rm -rf /tmp/creator-workflows
docker cp "$HERE/workflows" "$N8N_CONTAINER:/tmp/creator-workflows"
docker exec "$N8N_CONTAINER" n8n import:workflow \
  --separate --input=/tmp/creator-workflows
docker exec "$N8N_CONTAINER" rm -rf /tmp/creator-workflows

say "done"
cat <<'NEXT'
Four things are still manual, and all four are deliberate:

  1. The Postgres credential, in the n8n UI. Assign it to every Postgres node.
     Scripting it would mean writing the password out in plaintext first.
  2. The environment variables from n8n/.env.example, in Coolify, on the n8n
     service. NODE_FUNCTION_ALLOW_BUILTIN=crypto is the one that fails at
     runtime rather than at import, so the workflow looks healthy until it
     runs.
  3. Activating the workflows. Creating a resource is not deploying it, and
     importing a workflow is not activating it.
  4. Making it fail on purpose. See the six cases in n8n/README.md. An
     automation whose failure path has never run is an automation whose
     failure path does not work, and that is half of what is in this
     directory.
NEXT
