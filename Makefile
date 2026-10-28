# de-agent demo platform (Acme Shop, fictional). Requires docker compose.
COMPOSE = docker compose -f demo/docker-compose.yml --env-file demo/.env
DEA ?= cli/.venv/bin/dea
N ?=

.PHONY: demo-up demo-down demo-reset demo-snapshot fault-inject fault-revert

demo/.env:
	cp demo/.env.example demo/.env

demo-up: demo/.env
	$(COMPOSE) up -d --build --wait --wait-timeout 420
	@echo "airflow: http://localhost:$${DEMO_AF_PORT:-8085} (agent_ro / see demo/.env)"

demo-down:
	$(COMPOSE) down

demo-reset:
	$(COMPOSE) down -v
	rm -rf demo/dbt/acme/target demo/dbt/acme/logs

demo-snapshot: demo/.env
	cd demo && set -a && . ./.env && set +a && DEA_HUB=$(CURDIR)/demo ../$(DEA) snapshot

fault-inject:
	@test -n "$(N)" || (echo "usage: make fault-inject N=01"; exit 2)
	bash demo/faults/$(N)_*/inject.sh

fault-revert:
	@test -n "$(N)" || (echo "usage: make fault-revert N=01"; exit 2)
	bash demo/faults/$(N)_*/revert.sh
