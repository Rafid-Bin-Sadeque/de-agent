# de-agent demo platform (Acme Shop, fictional). Requires docker compose.
COMPOSE = docker compose -f demo/docker-compose.yml --env-file demo/.env
# dea: the repo venv if it exists, else whatever `dea` is on PATH (override: make demo-snapshot DEA=/path/to/dea)
DEA ?= $(shell if [ -x '$(CURDIR)/cli/.venv/bin/dea' ]; then echo '$(CURDIR)/cli/.venv/bin/dea'; else echo dea; fi)
D ?=
N ?=

.PHONY: demo demo-up demo-warm demo-down demo-reset demo-snapshot dag-trigger fault-inject fault-revert

demo/.env:
	cp demo/.env.example demo/.env

demo-up: demo/.env
	$(COMPOSE) up -d --build --wait --wait-timeout 420
	@echo "airflow: http://localhost:$${DEMO_AF_PORT:-8085} (agent_ro / see demo/.env)"

# one command for newcomers: start the stack, then run the first extract_shop -> dbt_build cycle
demo: demo-up demo-warm

# trigger extract_shop and wait for the first successful dbt_build (it writes dbt's manifest.json, run_results.json
# and sources.json into demo/dbt/acme/target, which `dea scan` and the dbt source in `dea snapshot` read)
demo-warm: demo/.env
	@echo "warming up: triggering extract_shop, waiting for the first dbt_build success (max 10 min)"
	@i=0; until $(COMPOSE) exec -T airflow airflow dags trigger extract_shop >/dev/null 2>&1; do \
	  i=$$((i+1)); [ $$i -le 30 ] || { echo "demo-warm: could not trigger extract_shop (is the stack up? try: make demo-up)"; exit 1; }; sleep 5; done
	@start=$$(date +%s); \
	while :; do \
	  el=$$(( $$(date +%s) - start )); \
	  if $(COMPOSE) exec -T airflow airflow dags list-runs -d dbt_build -o json 2>/dev/null | grep -Eq '"state": *"success"'; then \
	    echo "demo-warm: dbt_build succeeded after $${el}s"; break; fi; \
	  [ $$el -lt 600 ] || { echo "demo-warm: timed out after 10 min waiting for dbt_build. Check: $(COMPOSE) logs airflow"; exit 1; }; \
	  echo "  ...waiting for dbt_build ($${el}s)"; sleep 10; \
	done
	@ls demo/dbt/acme/target/manifest.json demo/dbt/acme/target/run_results.json demo/dbt/acme/target/sources.json

demo-down:
	$(COMPOSE) down

demo-reset:
	$(COMPOSE) down -v
	rm -rf demo/dbt/acme/target demo/dbt/acme/logs

demo-snapshot: demo/.env
	cd demo && set -a && . ./.env && set +a && DEA_HUB=$(CURDIR)/demo \
	  $(if $(findstring /,$(DEA)),$(abspath $(DEA)),$(DEA)) snapshot

# make dag-trigger D=extract_shop : run an Airflow DAG now (extract_shop otherwise runs hourly)
dag-trigger:
	@test -n "$(D)" || (echo "usage: make dag-trigger D=extract_shop"; exit 2)
	$(COMPOSE) exec -T airflow airflow dags trigger $(D)

fault-inject:
	@test -n "$(N)" || (echo "usage: make fault-inject N=01"; exit 2)
	bash demo/faults/$(N)_*/inject.sh

fault-revert:
	@test -n "$(N)" || (echo "usage: make fault-revert N=01"; exit 2)
	bash demo/faults/$(N)_*/revert.sh
