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

-- Routines (v0.3 demo): one plpgsql procedure writing analytics.daily_orders, a SQL function, a view and a trigger.
-- The export_daily_orders DAG runs `CALL analytics.refresh_orders()`; `dea impact raw.orders` walks table -> procedure -> DAG.
CREATE TABLE analytics.daily_orders (
  order_date date PRIMARY KEY,
  orders integer NOT NULL,
  revenue numeric(14,2) NOT NULL,
  refreshed_at timestamptz
);
ALTER TABLE analytics.daily_orders OWNER TO etl;
GRANT SELECT ON analytics.daily_orders TO readonly_agent;

CREATE FUNCTION analytics.order_total(p_order_id integer) RETURNS numeric
LANGUAGE sql STABLE AS $$
  SELECT coalesce(sum(quantity * unit_price), 0) FROM raw.order_items WHERE order_id = p_order_id
$$;

CREATE VIEW analytics.v_open_orders AS
  SELECT id, customer_id, total_amount, created_at FROM raw.orders WHERE status = 'open';

CREATE FUNCTION analytics.daily_orders_stamp() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.refreshed_at := now();
  RETURN NEW;
END
$$;

CREATE TRIGGER daily_orders_stamp BEFORE INSERT OR UPDATE ON analytics.daily_orders
  FOR EACH ROW EXECUTE FUNCTION analytics.daily_orders_stamp();

CREATE PROCEDURE analytics.refresh_orders()
LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM analytics.daily_orders WHERE order_date = current_date;
  INSERT INTO analytics.daily_orders (order_date, orders, revenue)
  SELECT current_date, count(*), coalesce(sum(total_amount), 0)
  FROM raw.orders
  WHERE created_at >= current_date;
END
$$;

ALTER FUNCTION analytics.order_total(integer) OWNER TO etl;
ALTER VIEW analytics.v_open_orders OWNER TO etl;
ALTER FUNCTION analytics.daily_orders_stamp() OWNER TO etl;
ALTER PROCEDURE analytics.refresh_orders() OWNER TO etl;
