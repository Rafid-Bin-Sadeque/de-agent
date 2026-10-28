select id as event_id, customer_id, event_type, created_at, updated_at
from {{ source('raw', 'events') }}
