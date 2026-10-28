-- fictional Acme Shop warehouse objects
create table etl.audit_log (id bigint, note text);

create or replace view analytics.v_orders as
select o.id, o.customer_id from raw.orders o join raw.customers c on c.id = o.customer_id;

create or replace procedure etl.refresh_orders()
language plpgsql
as $$
begin
  insert into raw.orders select * from staging.orders_incoming;
  if found then
    insert into etl.audit_log(note) values ('refreshed');
  end if;
end;
$$;

create or replace function etl.rollup_sales() returns void
language sql
as $body$
  insert into analytics.sales_rollup
  select order_date, sum(total_amount) from raw.orders group by 1;
$body$;
