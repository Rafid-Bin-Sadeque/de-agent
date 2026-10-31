-- fictional Acme Shop warehouse procedures
create table etl.load_state (job text, last_run timestamp);
create table etl.run_log (id bigint);

create or replace procedure etl.sp_refresh_orders()
language plpgsql
as $$
begin
  insert into raw.orders select * from staging.orders_stage;
  update etl.load_state set last_run = now() where job = 'orders';
end;
$$;

create or replace procedure etl.sp_refresh_customers()
language plpgsql
as $$
begin
  insert into raw.customers select * from staging.customers_stage;
  update etl.load_state set last_run = now() where job = 'customers';
end;
$$;

create or replace procedure etl.sp_nightly_a()
language plpgsql
as $$
begin
  insert into analytics.daily_orders select order_date, count(*) from raw.orders group by 1;
end;
$$;

create or replace procedure etl.sp_nightly_b()
language plpgsql
as $$
begin
  insert into analytics.daily_customers select signup_date, count(*) from raw.customers group by 1;
end;
$$;

create or replace procedure etl.sp_audit()
language plpgsql
as $$
begin
  insert into etl.run_log select 1;
end;
$$;

create or replace procedure etl.sp_monitor()
language plpgsql
as $$
begin
  insert into analytics.stale_jobs select job from etl.load_state where last_run < now() - interval '1 day';
end;
$$;
