select c.customer_id, c.email, c.country, c.created_at,
       count(o.order_id) as orders_count,
       coalesce(sum(o.total_amount), 0) as lifetime_value
from {{ ref('stg_customers') }} c
left join {{ ref('stg_orders') }} o on o.customer_id = c.customer_id
group by 1, 2, 3, 4
