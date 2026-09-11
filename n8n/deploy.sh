#!/usr/bin/env bash
#
# Put the workflows and their schema onto the VPS.
#
# Run it ON the box, over ssh, so the production password never leaves it:
#
#   scp -r -i ~/.ssh/coolify_vps n8n ubuntu@51.195.223.171:/tmp/creator-n8n
#   ssh -i ~/.ssh/coolify_vps ubuntu@51.195.223.171
#   CREATOR_DB_PASSWORD="$(cat ~/.creator_prod_password)" #     bash /tmp/creator-n8n/deploy.sh
#
# ubuntu, not root, and docker needs sudo. The password lives in
# ~/.creator_prod_password on the box, chmod 600, and nowhere else -- not in
# this repository and not in Coolify.
#
# Idempotent on purpose. The role and database are created only if absent, the
# schema is CREATE TABLE IF NOT EXISTS throughout, the golden set is an upsert,
# and the workflows have fixed ids so importing twice updates rather than
# duplicates. Running it again after a partial failure is the normal way to
# use it, which is the only kind of deploy script anyone actually trusts.
#
# What it does NOT do: create the n8n credentials.
#
# The Postgres one exists already, imported once with `n8n import:credentials`
# under the fixed id creatorPgCred001, which is what every Postgres node in
# the workflows references. Re-creating it means writing the password to a
# plaintext file first, so it is a one-off done by hand rather than a step
# that runs on every deploy.
#
# The Header Auth one for the replay webhook has to be made in the UI. It
# guards an endpoint that re-runs work which sends email.

set -euo pipefail

# Coolify names containers by resource uuid, not by the service name you
# typed. These are the real ones on this box; find them with `docker ps`.
PG_CONTAINER="${PG_CONTAINER:-yhnvfpxjld6w2dthn71qgcwl}"
N8N_CONTAINER="${N8N_CONTAINER:-n8n-juqfegd2caaahmtogi2yxgbi}"
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
for f in 01-schema.sql 02-golden-set.sql 03-config.sql; do
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
Two things are still manual, and both are deliberate:

  1. The Header Auth credential for the replay webhook in 04, in the n8n UI.
     It guards an endpoint that re-runs work which sends email, and it should
     not exist in a file anywhere.
  2. Making it fail on purpose. See the six cases in n8n/README.md. An
     automation whose failure path has never run is an automation whose
     failure path does not work, and that is half of what is in this
     directory.

Folder placement and credential assignment both survive a re-import, so the
grouping in the n8n UI is not something this script has to rebuild.

Activation does not survive, and that is not optional to fix -- a deploy
silently stops every automation until you run these:

  n8n update:workflow --id creatorRules0000 --active=true
  n8n update:workflow --id creatorPipeline1 --active=true
  n8n update:workflow --id creatorErrors003 --active=true
  n8n update:workflow --id creatorEval00005 --active=true
  docker restart <n8n container>

00 and 01 have no trigger of their own and still have to be active. In n8n 2.x
a call into an inactive workflow fails with "Workflow is not active and cannot
be executed", which reads like a permissions problem and is not.

Leave 01a, 02, 04 and 06 inactive until their endpoints in the config table
point at something real. An active workflow aimed at example.invalid produces
genuine failures that teach nothing and fill the dead letter table.
NEXT
