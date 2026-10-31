"""Hand-built fixtures for the built-in packs: documented response shapes with FICTIONAL values (no real system was queried,
except for postgres-lite and mysql, which are also run against live containers in test_packs_live.py).

NOW is the injected clock. For each pack: `options` (the source's options:), and `responses` keyed `<section>` or
`<section>@<item>` (for_each). sql packs: list of rows; cli packs: the JSON the command prints; http packs: the response body.
"""
NOW = "2026-10-01T12:00:00Z"

FIXTURES = {
    "postgres-lite": {"options": {}, "responses": {
        "long_running_sessions": [
            {"pid": 4101, "user": "etl_user", "state": "active", "wait_event_type": "IO", "duration_s": 5400, "query": "select count(*) from shop.orders o join shop.items i on i.order_id = o.id"},
            {"pid": 4102, "user": "bi_reader", "state": "active", "wait_event_type": None, "duration_s": 420, "query": "select * from shop.customers"}],
        "blocked_sessions": [
            {"blocked_pid": 4200, "blocker_pid": 4101, "wait_s": 95, "blocked_query": "update shop.orders set status = 'x' where id = 1", "blocker_query": "select ... for share"}],
        "dead_tuples": [
            {"schema": "shop", "table": "orders", "n_live_tup": 180000, "n_dead_tup": 120000, "dead_pct": 40.0, "last_autovacuum": "2026-09-30T02:15:00Z"},
            {"schema": "shop", "table": "customers", "n_live_tup": 120000, "n_dead_tup": 5000, "dead_pct": 4.0, "last_autovacuum": "2026-10-01T03:00:00Z"}],
        "table_sizes": [
            {"schema": "shop", "table": "orders", "est_bytes": 524288000, "est_rows": 180000},
            {"schema": "shop", "table": "customers", "est_bytes": 41943040, "est_rows": 120000}]}},
    "mysql": {"options": {}, "responses": {
        "long_processlist": [{"id": 88, "user": "etl", "db": "shop", "command": "Query", "seconds": 640, "state": "executing", "info": "select sleep(700)"}],
        "innodb_lock_waits": [{"wait_age_secs": 12, "locked_table": "`shop`.`orders`", "waiting_pid": 91, "blocking_pid": 88, "waiting_query": "update orders set note = 'a'", "blocking_query": None}],
        "replica_status": [{"Source_Host": "db-primary.example.internal", "Replica_IO_Running": "Yes", "Replica_SQL_Running": "No", "Seconds_Behind_Source": None, "Last_IO_Error": "", "Last_SQL_Error": "Duplicate entry '7' for key 'PRIMARY'"}],
        "top_tables": [
            {"table_schema": "shop", "table_name": "orders", "total_bytes": 83886080, "approx_rows": 250000},
            {"table_schema": "shop", "table_name": "reviews", "total_bytes": 16384000, "approx_rows": 40000}]}},
    "snowflake": {"options": {"warehouses": ["ANALYTICS_WH", "LOAD_WH"], "databases": ["SHOP_DB"]}, "responses": {
        "warehouses": [
            {"name": "ANALYTICS_WH", "state": "STARTED", "type": "STANDARD", "size": "Medium", "auto_suspend": 600, "running": 2, "queued": 0},
            {"name": "LOAD_WH", "state": "SUSPENDED", "type": "STANDARD", "size": "Small", "auto_suspend": 0, "running": 0, "queued": 0}],
        "failed_queries_24h": [
            {"query_id": "01b2-0001", "user_name": "ETL_USER", "warehouse_name": "LOAD_WH", "error_code": "000604", "error_message": "Monthly credit quota exceeded", "start_time": "2026-10-01T03:12:00Z"},
            {"query_id": "01b2-0002", "user_name": "BI_USER", "warehouse_name": "ANALYTICS_WH", "error_code": "002003", "error_message": "Object 'SHOP_DB.PUBLIC.ORDERS_V2' does not exist", "start_time": "2026-10-01T09:40:00Z"}],
        "queued_queries_24h": [{"warehouse_name": "ANALYTICS_WH", "queued_queries": 14, "avg_queued_s": 42.5}],
        "credits_24h": [{"warehouse_name": "ANALYTICS_WH", "credits_used": 18.4}, {"warehouse_name": "LOAD_WH", "credits_used": 3.1}],
        "task_failures_24h": [{"name": "REFRESH_ORDERS", "database_name": "SHOP_DB", "schema_name": "PUBLIC", "state": "FAILED", "error_code": "003001", "error_message": "SQL access control error", "scheduled_time": "2026-10-01T04:00:00Z"}],
        "pipe_failures_24h": [{"pipe_name": "SHOP_DB.PUBLIC.ORDERS_PIPE", "table_name": "ORDERS_RAW", "file_name": "orders/2026/10/01/part-0007.csv", "status": "Load failed", "first_error_message": "Numeric value 'abc' is not recognized", "last_load_time": "2026-10-01T06:30:00Z"}],
        "top_storage_tables": [{"table_catalog": "SHOP_DB", "table_schema": "PUBLIC", "table_name": "ORDERS_RAW", "active_bytes": 9663676416, "time_travel_bytes": 1073741824, "failsafe_bytes": 536870912}]}},
    "bigquery": {"options": {"region": "eu", "datasets": ["shop_dw"]}, "responses": {
        "job_failures_24h": [
            {"job_id": "bquxjob_1a2b", "user_email": "etl@example-shop.iam.gserviceaccount.com", "reason": "quotaExceeded", "message": "Quota exceeded: too many concurrent queries", "creation_time": "2026-10-01T05:00:00Z"},
            {"job_id": "bquxjob_3c4d", "user_email": "analyst@example.com", "reason": "invalidQuery", "message": "Unrecognized name: revenue at [3:8]", "creation_time": "2026-10-01T08:30:00Z"}],
        "top_bytes_billed_24h": [{"job_id": "bquxjob_9z8y", "user_email": "analyst@example.com", "total_bytes_billed": 5497558138880, "slot_seconds": 1830.5, "creation_time": "2026-10-01T07:10:00Z"}],
        "slot_use_hourly": [
            {"hour_start": "2026-10-01T11:00:00Z", "slot_hours": 4.25, "jobs": 38},
            {"hour_start": "2026-10-01T10:00:00Z", "slot_hours": 2.5, "jobs": 21}],
        "largest_tables": [{"table_schema": "shop_dw", "table_name": "fct_orders", "total_logical_bytes": 214748364800, "active_logical_bytes": 161061273600}],
        "stale_tables": [{"table_schema": "shop_dw", "table_name": "dim_promo_2024", "storage_last_modified_time": "2026-09-12T00:00:00Z"}]}},
    "databricks-system": {"options": {"workspace_id": "1234567890123456"}, "responses": {
        "job_failures_24h": [{"job_id": 701, "run_id": 90001, "result_state": "FAILED", "termination_code": "RUN_EXECUTION_ERROR", "period_start_time": "2026-10-01T02:00:00Z"}],
        "usage_24h": [{"sku_name": "STANDARD_JOBS_COMPUTE", "usage_unit": "DBU", "usage_quantity": 412.5}, {"sku_name": "PREMIUM_SQL_COMPUTE", "usage_unit": "DBU", "usage_quantity": 96.0}],
        "failed_statements_24h": [{"statement_id": "01ef-aaaa-0001", "executed_by": "analyst@example.com", "error_message": "[TABLE_OR_VIEW_NOT_FOUND] The table or view `shop`.`orders_v3` cannot be found", "start_time": "2026-10-01T10:05:00Z"}]}},
    "mssql": {"options": {}, "responses": {
        "blocking_chains": [{"blocked_session": 61, "blocker_session": 55, "wait_type": "LCK_M_X", "wait_s": 420, "database_name": "ShopDb"}],
        "long_requests": [{"session_id": 55, "status": "suspended", "command": "UPDATE", "elapsed_s": 1260, "database_name": "ShopDb", "statement": "update dbo.Orders set Status = 2 where CreatedAt < '2026-01-01'"}],
        "agent_job_failures_24h": [{"job_name": "Nightly Orders Load", "run_status": 0, "message": "The job failed. The Job was invoked by Schedule 4 (Nightly).", "run_at": "2026-10-01T01:00:00Z"}],
        "top_tables": [{"schema_name": "dbo", "table_name": "Orders", "total_bytes": 2147483648, "approx_rows": 5200000}]}},
    "databricks": {"options": {"job_ids": ["701", "702"], "cluster_ids": ["0901-123456-abcd1234"], "warehouse_ids": ["a1b2c3d4e5f6"], "long_run_hours": 6}, "responses": {
        "failed_runs_24h@701": [
            {"run_id": 90001, "job_id": 701, "run_name": "nightly-orders", "state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED", "state_message": "Task load_orders failed"}, "start_time": 1759286400000},
            {"run_id": 90002, "job_id": 701, "run_name": "nightly-orders", "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS", "state_message": ""}, "start_time": 1759290000000}],
        "failed_runs_24h@702": [],
        "active_runs@701": [{"run_id": 90003, "job_id": 701, "run_name": "nightly-orders", "state": {"life_cycle_state": "RUNNING"}, "start_time": 1759240800000}],
        "active_runs@702": [],
        "clusters@0901-123456-abcd1234": {"cluster_id": "0901-123456-abcd1234", "cluster_name": "etl-shared", "state": "TERMINATED", "termination_reason": {"code": "INSTANCE_UNREACHABLE"}, "state_message": "Instance unreachable"},
        "sql_warehouses@a1b2c3d4e5f6": {"id": "a1b2c3d4e5f6", "name": "bi-small", "state": "RUNNING", "cluster_size": "Small", "auto_stop_mins": 10}}},
    "gcp": {"options": {"project": "example-shop-prod", "composer_locations": ["europe-west1"], "dataflow_regions": ["europe-west1"], "bucket_prefixes": ["example-shop-landing/orders"]}, "responses": {
        "composer_environments@europe-west1": [
            {"name": "projects/example-shop-prod/locations/europe-west1/environments/shop-airflow", "state": "RUNNING", "config": {"softwareConfig": {"imageVersion": "composer-2.9.7-airflow-2.9.3"}}, "updateTime": "2026-09-20T08:00:00Z"},
            {"name": "projects/example-shop-prod/locations/europe-west1/environments/shop-airflow-test", "state": "ERROR", "config": {"softwareConfig": {"imageVersion": "composer-2.9.7-airflow-2.9.3"}}, "updateTime": "2026-09-28T08:00:00Z"}],
        "dataflow_failed_24h@europe-west1": [
            {"id": "2026-10-01_00_00_00-1111", "name": "orders-stream", "currentState": "JOB_STATE_FAILED", "createTime": "2026-10-01T00:00:00Z"},
            {"id": "2026-10-01_01_00_00-2222", "name": "orders-batch", "currentState": "JOB_STATE_DONE", "createTime": "2026-10-01T01:00:00Z"}],
        "pubsub_subscriptions": [
            {"name": "projects/example-shop-prod/subscriptions/orders-sub", "topic": "projects/example-shop-prod/topics/orders", "ackDeadlineSeconds": 60, "deadLetterPolicy": {"deadLetterTopic": "projects/example-shop-prod/topics/orders-dlq"}, "retainAckedMessages": False},
            {"name": "projects/example-shop-prod/subscriptions/clicks-sub", "topic": "projects/example-shop-prod/topics/clicks", "ackDeadlineSeconds": 10, "retainAckedMessages": False}],
        "newest_objects@example-shop-landing/orders": [
            {"name": "orders/2026/09/30/part-0001.json", "updated": "2026-09-30T23:10:00Z", "size": "1048576"},
            {"name": "orders/2026/10/01/part-0003.json", "updated": "2026-10-01T09:05:00Z", "size": "2097152"}]}},
    "kubernetes": {"options": {"namespaces": ["shop", "data"]}, "responses": {
        "pods_not_running@shop": {"items": [
            {"metadata": {"name": "api-7d9f-abcde"}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}},
            {"metadata": {"name": "worker-5c4b-xyz12"}, "status": {"phase": "Pending", "reason": None}},
            {"metadata": {"name": "etl-once-1a2b3"}, "status": {"phase": "Succeeded"}}]},
        "pods_not_running@data": {"items": [{"metadata": {"name": "spark-driver-1"}, "status": {"phase": "Failed", "reason": "Evicted"}}]},
        "crashloop_pods@shop": {"items": [
            {"metadata": {"name": "worker-5c4b-xyz12"}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 14, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}},
            {"metadata": {"name": "api-7d9f-abcde"}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}}]},
        "crashloop_pods@data": {"items": []},
        "failed_jobs@shop": {"items": [{"metadata": {"name": "reindex-20261001"}, "status": {"failed": 3, "startTime": "2026-10-01T02:00:00Z"}}]},
        "failed_jobs@data": {"items": [{"metadata": {"name": "compact-20261001"}, "status": {"succeeded": 1, "startTime": "2026-10-01T03:00:00Z"}}]},
        "cronjobs@shop": {"items": [
            {"metadata": {"name": "nightly-export"}, "spec": {"schedule": "0 2 * * *", "suspend": False}, "status": {"lastScheduleTime": "2026-10-01T02:00:00Z", "lastSuccessfulTime": "2026-09-30T02:00:30Z"}},
            {"metadata": {"name": "weekly-report"}, "spec": {"schedule": "0 6 * * 1", "suspend": False}, "status": {"lastScheduleTime": "2026-09-28T06:00:00Z"}}]},
        "cronjobs@data": {"items": []}}},
    "dagster": {"options": {"max_runs": 50}, "responses": {
        "failed_runs_24h": {"data": {"runsOrError": {"__typename": "Runs", "results": [
            {"runId": "7c1e0a52-0000-4a00-9000-000000000001", "jobName": "orders_daily", "status": "FAILURE", "startTime": 1759287000.5, "endTime": 1759287600.0}]}}},
        "schedules": {"data": {"workspaceOrError": {"__typename": "Workspace", "locationEntries": [
            {"name": "shop_code", "locationOrLoadError": {"repositories": [{"name": "__repository__", "schedules": [
                {"name": "orders_daily_schedule", "scheduleState": {"status": "RUNNING"}},
                {"name": "legacy_export_schedule", "scheduleState": {"status": "STOPPED"}}]}]}}]}}},
        "sensors": {"data": {"workspaceOrError": {"__typename": "Workspace", "locationEntries": [
            {"name": "shop_code", "locationOrLoadError": {"repositories": [{"name": "__repository__", "sensors": [
                {"name": "new_file_sensor", "sensorState": {"status": "RUNNING"}}]}]}}]}}}}},
    "fivetran": {"options": {"group_ids": ["grp_example_1"], "stale_hours": 24}, "responses": {
        "connectors@grp_example_1": {"code": "Success", "data": {"items": [
            {"id": "conn_orders", "schema": "shop_orders", "service": "postgres", "paused": False, "status": {"sync_state": "scheduled", "setup_state": "connected"}, "succeeded_at": "2026-10-01T10:00:00Z", "failed_at": None},
            {"id": "conn_ads", "schema": "ads_spend", "service": "google_ads", "paused": False, "status": {"sync_state": "scheduled", "setup_state": "connected"}, "succeeded_at": "2026-09-29T10:00:00Z", "failed_at": "2026-09-30T10:00:00Z"},
            {"id": "conn_crm", "schema": "crm", "service": "salesforce", "paused": True, "status": {"sync_state": "paused", "setup_state": "connected"}, "succeeded_at": "2026-09-01T10:00:00Z", "failed_at": None},
            {"id": "conn_new", "schema": "new_src", "service": "mysql", "paused": False, "status": {"sync_state": "scheduled", "setup_state": "incomplete"}, "succeeded_at": None, "failed_at": None}],
            "next_cursor": None}}}},
}
