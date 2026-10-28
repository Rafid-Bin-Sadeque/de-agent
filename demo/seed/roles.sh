#!/bin/bash
# Source-DB roles: etl_reader (extract_shop), readonly_agent (de-agent), app (app-sim writes), etl (fault scripts).
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE etl_reader LOGIN PASSWORD '${POSTGRES_PASSWORD}';
CREATE ROLE readonly_agent LOGIN PASSWORD '${DEMO_WH_PASSWORD}';
CREATE ROLE etl LOGIN PASSWORD '${DEMO_ETL_PASSWORD}';
GRANT pg_read_all_stats TO readonly_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO etl_reader, readonly_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO etl_reader, readonly_agent;
-- the fault scripts connect as etl and need DDL/DML on the demo tables
GRANT ALL ON SCHEMA public TO etl;
GRANT ALL ON ALL TABLES IN SCHEMA public TO etl;
SQL
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
ALTER TABLE customers OWNER TO etl;
ALTER TABLE products OWNER TO etl;
ALTER TABLE orders OWNER TO etl;
ALTER TABLE order_items OWNER TO etl;
ALTER TABLE events OWNER TO etl;
SQL
