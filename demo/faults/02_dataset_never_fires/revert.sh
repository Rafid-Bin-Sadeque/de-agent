#!/usr/bin/env bash
. "$(dirname "$0")/../_lib.sh"
af dags unpause extract_shop 2>&1 | tail -1
say "02 reverted: extract_shop unpaused"
