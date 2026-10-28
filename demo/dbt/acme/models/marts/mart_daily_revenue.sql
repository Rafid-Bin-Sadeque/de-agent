{{ config(materialized='incremental', unique_key='order_date', incremental_strategy='delete+insert') }}
{#- A plain run (dbt_build) recomputes every day. A ranged rebuild: --vars '{start_date: 2026-10-01, end_date: 2026-10-03}'
    recomputes only those days (inclusive, by created_at date); the delete+insert on order_date makes it idempotent. -#}
select date_trunc('day', created_at)::date as order_date,
       count(*) as orders,
       sum(total_amount) filter (where status not in ('cancelled', 'refunded')) as revenue
from {{ ref('fct_orders') }}
{% if is_incremental() %}
where 1 = 1
  {% if var('start_date', none) %}and created_at >= '{{ var("start_date") }}'::date{% endif %}
  {% if var('end_date', none) %}and created_at < '{{ var("end_date") }}'::date + 1{% endif %}
{% endif %}
group by 1
