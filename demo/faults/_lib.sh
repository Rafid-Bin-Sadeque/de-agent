# Shared helpers for the fault scripts. Source this file; run from anywhere. Demo stack only (localhost containers).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/demo/docker-compose.yml" --env-file "$ROOT/demo/.env")
# psql as the demo `etl` role over the container's local socket (trusted inside the container)
src_sql() { "${COMPOSE[@]}" exec -T source-db psql -X -q -U etl -d shop -v ON_ERROR_STOP=1 "$@"; }
wh_sql()  { "${COMPOSE[@]}" exec -T warehouse psql -X -q -U etl -d warehouse -v ON_ERROR_STOP=1 "$@"; }
af()      { "${COMPOSE[@]}" exec -T airflow airflow "$@"; }
say()     { echo "[fault] $*"; }
