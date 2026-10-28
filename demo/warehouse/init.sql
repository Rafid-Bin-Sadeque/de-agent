-- Warehouse: schemas raw / staging / analytics. Runs as wh_owner in db `warehouse`.
CREATE SCHEMA raw AUTHORIZATION etl;
CREATE SCHEMA staging AUTHORIZATION etl;
CREATE SCHEMA analytics AUTHORIZATION etl;
GRANT USAGE ON SCHEMA raw, staging, analytics TO readonly_agent;
GRANT CREATE ON DATABASE warehouse TO etl;
ALTER DEFAULT PRIVILEGES FOR ROLE etl IN SCHEMA raw, staging, analytics GRANT SELECT ON TABLES TO readonly_agent;

-- raw tables mirror the source columns. No primary keys (a landing layer): extract_shop upserts by id.
CREATE TABLE raw.customers   (id integer, email text, full_name text, country text, created_at timestamptz, updated_at timestamptz);
CREATE TABLE raw.products    (id integer, sku text, name text, category text, unit_price numeric(10,2), created_at timestamptz, updated_at timestamptz);
CREATE TABLE raw.orders      (id integer, customer_id integer, status text, total_amount numeric(12,2), created_at timestamptz, updated_at timestamptz);
CREATE TABLE raw.order_items (id integer, order_id integer, product_id integer, quantity integer, unit_price numeric(10,2), created_at timestamptz, updated_at timestamptz);
CREATE TABLE raw.events      (id bigint, customer_id integer, event_type text, payload text, created_at timestamptz, updated_at timestamptz);
CREATE INDEX customers_id_idx ON raw.customers (id);
CREATE INDEX products_id_idx ON raw.products (id);
CREATE INDEX orders_id_idx ON raw.orders (id);
CREATE INDEX orders_updated_at_idx ON raw.orders (updated_at);
CREATE INDEX order_items_id_idx ON raw.order_items (id);
CREATE INDEX events_id_idx ON raw.events (id);
CREATE TABLE raw._watermarks (
  table_name text PRIMARY KEY,
  high_watermark timestamptz NOT NULL DEFAULT '1970-01-01+00',
  rows_loaded bigint NOT NULL DEFAULT 0,
  last_check_rows bigint,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO raw._watermarks (table_name) VALUES ('customers'), ('products'), ('orders'), ('order_items'), ('events');
ALTER TABLE raw.customers OWNER TO etl; ALTER TABLE raw.products OWNER TO etl; ALTER TABLE raw.orders OWNER TO etl;
ALTER TABLE raw.order_items OWNER TO etl; ALTER TABLE raw.events OWNER TO etl; ALTER TABLE raw._watermarks OWNER TO etl;
GRANT SELECT ON ALL TABLES IN SCHEMA raw TO readonly_agent;
