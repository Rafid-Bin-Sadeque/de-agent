{{ config(materialized='incremental', unique_key='order_id') }}
with items as (
    select order_id, count(*) as item_count, sum(line_amount) as items_amount
    from {{ ref('stg_order_items') }}
    group by 1
)
select o.order_id, o.customer_id, o.status, o.total_amount,
       coalesce(i.item_count, 0) as item_count, coalesce(i.items_amount, 0) as items_amount,
       o.created_at, o.updated_at
from {{ ref('stg_orders') }} o
left join items i on i.order_id = o.order_id
{% if is_incremental() %}
where o.updated_at >= (select max(updated_at) from {{ this }})
{% endif %}
