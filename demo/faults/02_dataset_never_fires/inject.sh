#!/usr/bin/env bash
# Fault 02: someone pauses extract_shop, so the raw datasets never update and dbt_build never fires.
. "$(dirname "$0")/../_lib.sh"
af dags pause extract_shop 2>&1 | tail -1
say "02 injected: extract_shop is paused"
