"""Small hub-local packs for tests of the GENERIC pack machinery (independent of the built-in packs)."""
from de_agent.packs.loader import set_hub_hint

PACKS = {
    "pgtest": """pack: pgtest
kind: sql
dialect: postgres
requires: {extra: pg}
sections:
  - name: one
    sql: select 1 as ok
""",
    "mytest": """pack: mytest
kind: sql
dialect: mysql
requires: {extra: mysql}
sections:
  - name: one
    sql: select 1 as ok
""",
    "snowtest": """pack: snowtest
kind: sql
dialect: snowflake
requires: {extra: snowflake}
params:
  warehouses: {type: list, required: true}
sections:
  - name: one
    sql: select query_id from q where warehouse_name in :warehouses
    key: query_id
""",
    "fttest": """pack: fttest
kind: http
params:
  group_ids: {type: list, required: true}
sections:
  - name: connectors
    get: /v1/groups/{{item}}/connectors?limit=100
    for_each: group_ids
    item_as: group_id
    select: "data.items"
    fields: {id: id, schema: schema, service: service, paused: paused, setup_state: status.setup_state}
    columns: [group_id, id, schema, service, paused, setup_state]
    key: id
""",
    "dgtest": """pack: dgtest
kind: http
sections:
  - name: failed_runs_24h
    graphql: |
      query { runsOrError(filter: {statuses: [FAILURE], createdAfter: {{since_24h_epoch}}}, limit: 50) { __typename ... on Runs { results { runId jobName status } } } }
    select: "data.runsOrError.results"
    fields: {run_id: runId, job_name: jobName, status: status}
    key: run_id
    signals:
      - rows: "count > 0"
        message: "{count} failed run(s)"
""",
}


def install(tmp_path):
    """Write PACKS into <tmp>/stdhub/packs and make that the hub packs/ is resolved from."""
    hub = tmp_path / "stdhub"
    (hub / "packs").mkdir(parents=True, exist_ok=True)
    for name, text in PACKS.items():
        (hub / "packs" / f"{name}.yaml").write_text(text)
    (hub / "de-agent.yaml").write_text("version: 1\n")
    set_hub_hint(hub)
    return hub
