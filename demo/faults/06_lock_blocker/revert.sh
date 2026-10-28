#!/usr/bin/env bash
. "$(dirname "$0")/../_lib.sh"
wh_sql -c "select pg_terminate_backend(pid) from pg_stat_activity where application_name = 'dea_fault_06'" >/dev/null
say "06 reverted: lock holder terminated (the fault's own session only)"
