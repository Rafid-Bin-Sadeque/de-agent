select id as order_id, customer_id, status, total_amount, created_at, updated_at
from {{ source('raw', 'orders') }}
