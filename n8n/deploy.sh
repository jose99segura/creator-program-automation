#!/usr/bin/env bash
#
# Put the schema and the workflows onto the VPS. Run it ON the box, so the
# production password never leaves it:
#
#   scp -r -i ~/.ssh/coolify_vps n8n ubuntu@51.195.223.171:/tmp/creator-n8n
#   ssh -i ~/.ssh/coolify_vps ubuntu@51.195.223.171
#   CREATOR_DB_PASSWORD="$(cat ~/.creator_prod_password)" sudo -E bash /tmp/creator-n8n/deploy.sh
#
# Safe to run again: role and database are created only if missing, the SQL is
# IF NOT EXISTS, and the workflows have fixed ids so a re-import updates them.
#
# Not done here: the two n8n credentials, created once by hand -- the Postgres
# one (id creatorPgCred001) and the dashboard's basic auth (creatorDashAuth01).

set -euo pipefail

# Coolify names containers by resource uuid. Find them with `docker ps`.
PG_CONTAINER="${PG_CONTAINER:-yhnvfpxjld6w2dthn71qgcwl}"
N8N_CONTAINER="${N8N_CONTAINER:-n8n-juqfegd2caaahmtogi2yxgbi}"
N8N_DB_CONTAINER="${N8N_DB_CONTAINER:-postgresql-juqfegd2caaahmtogi2yxgbi}"
N8N_FOLDER_ID="${N8N_FOLDER_ID:-fldCreatorRoot}"
WORKFLOW_IDS="creatorPipeline1 creatorPayout002 creatorErrors003 creatorDash00007 creatorFakes0009"
WORKFLOW_IDS_SQL="'${WORKFLOW_IDS// /\',\'}'"
DB_NAME="${DB_NAME:-creator_prod}"
DB_ROLE="${DB_ROLE:-creator_prod}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${CREATOR_DB_PASSWORD:?set CREATOR_DB_PASSWORD to the password for the ${DB_ROLE} role}"

say() { printf '\n=== %s\n' "$*"; }

require_container() {
  docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true \
    || { echo "container '$1' is not running" >&2; exit 1; }
}

say "checking containers"
require_container "$PG_CONTAINER"
require_container "$N8N_CONTAINER"

psql_super() { docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres "$@"; }

say "role and database"
psql_super -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DB_ROLE}') THEN
    CREATE ROLE ${DB_ROLE} LOGIN PASSWORD '${CREATOR_DB_PASSWORD}';
  END IF;
END
\$\$;
SQL

if ! psql_super -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
  psql_super -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_ROLE};"
fi

say "schema and config"
# As the app role, so it owns every table it creates.
for f in 01-schema.sql 02-config.sql 04-fake-applicants.sql; do
  echo "-- $f"
  docker exec -i -e PGPASSWORD="$CREATOR_DB_PASSWORD" "$PG_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -U "$DB_ROLE" -d "$DB_NAME" < "$HERE/sql/$f"
done

say "tables not owned by ${DB_ROLE} (should be empty)"
psql_super -d "$DB_NAME" -c \
  "SELECT tablename, tableowner FROM pg_tables
    WHERE schemaname = 'public' AND tableowner <> '${DB_ROLE}';"

say "importing workflows"
docker exec "$N8N_CONTAINER" rm -rf /tmp/creator-workflows
docker cp "$HERE/workflows" "$N8N_CONTAINER:/tmp/creator-workflows"
docker exec "$N8N_CONTAINER" n8n import:workflow --separate --input=/tmp/creator-workflows
docker exec "$N8N_CONTAINER" rm -rf /tmp/creator-workflows

say "filing the workflows in one folder"
# All five live directly in "creator program", with no subfolders. Deleting a
# folder in n8n cascades to the workflows inside it, so never delete one that
# is not empty.
psql_n8n() { docker exec -i "$N8N_DB_CONTAINER" sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
psql_n8n <<SQL
INSERT INTO folder (id, name, "projectId")
SELECT '${N8N_FOLDER_ID}', 'creator program', "projectId"
  FROM shared_workflow WHERE "workflowId" = 'creatorPipeline1'
ON CONFLICT (id) DO NOTHING;
UPDATE workflow_entity SET "parentFolderId" = '${N8N_FOLDER_ID}'
 WHERE id IN (${WORKFLOW_IDS_SQL});
SQL

say "activating"
# A re-import leaves every workflow inactive.
for id in $WORKFLOW_IDS; do
  docker exec "$N8N_CONTAINER" n8n publish:workflow --id="$id"
done
docker restart "$N8N_CONTAINER"

say "done"
