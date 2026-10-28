# orders_etl--warehouse

## Contract
orders_etl owns analytics.orders; nothing else writes to it.

## Timing
The nightly load must finish before the morning reports read the table.

## Gotchas
A long-running report query can block the load's final swap.
