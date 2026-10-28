---
owner: data-team
domain: sales
updated: {{today}}
---
# orders_etl

## Purpose
Loads the shop's orders and customers into the analytics schema every night.

## Layout
One job per table; the orders job runs after the customers job.

## Conventions
Idempotent loads keyed by order id; reruns are always safe.

## Connections
Reads the shop database, writes analytics.orders. See [[orders_etl--warehouse]].

## Gotchas
- A late customers load makes orders appear without a customer name until the next run.
