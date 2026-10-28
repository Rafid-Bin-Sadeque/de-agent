-- Acme Shop OLTP schema (fictional). Every table carries updated_at.
CREATE TABLE customers (
  id          integer PRIMARY KEY,
  email       text NOT NULL,
  full_name   text NOT NULL,
  country     text NOT NULL,
  created_at  timestamptz NOT NULL,
  updated_at  timestamptz NOT NULL
);
CREATE TABLE products (
  id          integer PRIMARY KEY,
  sku         text NOT NULL,
  name        text NOT NULL,
  category    text NOT NULL,
  unit_price  numeric(10,2) NOT NULL,
  created_at  timestamptz NOT NULL,
  updated_at  timestamptz NOT NULL
);
CREATE TABLE orders (
  id           integer PRIMARY KEY,
  customer_id  integer NOT NULL REFERENCES customers(id),
  status       text NOT NULL,
  total_amount numeric(12,2) NOT NULL,
  created_at   timestamptz NOT NULL,
  updated_at   timestamptz NOT NULL
);
CREATE TABLE order_items (
  id          integer PRIMARY KEY,
  order_id    integer NOT NULL REFERENCES orders(id),
  product_id  integer NOT NULL REFERENCES products(id),
  quantity    integer NOT NULL,
  unit_price  numeric(10,2) NOT NULL,
  created_at  timestamptz NOT NULL,
  updated_at  timestamptz NOT NULL
);
CREATE TABLE events (
  id          bigint PRIMARY KEY,
  customer_id integer,
  event_type  text NOT NULL,
  payload     text,
  created_at  timestamptz NOT NULL,
  updated_at  timestamptz NOT NULL
);
CREATE INDEX orders_updated_at_idx ON orders (updated_at);
CREATE INDEX order_items_updated_at_idx ON order_items (updated_at);
CREATE INDEX events_updated_at_idx ON events (updated_at);
CREATE INDEX customers_updated_at_idx ON customers (updated_at);
CREATE INDEX products_updated_at_idx ON products (updated_at);
