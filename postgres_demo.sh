#!/usr/bin/env bash
# End to end on PostgreSQL: load the feeds, reconcile from the database, prove a second run changes nothing,
# run the Operations queries. load-db stands in for the feed adapters of a real firm (DESIGN.md, section 7).
#
# Usage:  ./postgres_demo.sh
#         RECON_DSN="host=... dbname=..." START_DB=0 ./postgres_demo.sh   (existing server)
set -euo pipefail
cd "$(dirname "$0")"

export RECON_DSN="${RECON_DSN:-host=localhost port=5432 user=recon password=recon dbname=recon}"
OUT="output/postgres"

[ "${START_DB:-1}" = "1" ] && docker compose up -d --wait

uv run --extra postgres python -m recon load-db
uv run --extra postgres python -m recon run --store postgres --source postgres --fresh --quiet --out "$OUT"

before=$(psql "$RECON_DSN" -tAc "select count(*) from recon_status_events")
uv run --extra postgres python -m recon run --store postgres --source postgres --quiet --out "$OUT" > /dev/null
after=$(psql "$RECON_DSN" -tAc "select count(*) from recon_status_events")
echo "status events before the second run: $before, after: $after"

psql "$RECON_DSN" -v record_id="${RECORD_ID:-2596}" -f sql/ops_queries.sql
