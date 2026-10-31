"""`dea pack new`: commented starter packs. Each one passes `dea pack check` as written."""
from __future__ import annotations

SQL = """# Pack scaffold (kind: sql). Reference: docs/packs.md
pack: {name}
version: 1
kind: sql
dialect: postgres            # postgres | redshift | mysql | snowflake | bigquery | tsql | databricks (how statements are classified)
requires: {{extra: pg}}        # pip extra that installs the driver: pip install 'de-agent[pg]'
validated: fixtures          # `fixtures` until you have run it against a real system; then `live`
description: One sentence on what this pack watches.
params: {{}}                   # values come from the source's options:  name: {{type: str|ident|int|bool|list, default: .., required: ..}}
sections:
  - name: example
    sql: |                   # exactly ONE read statement; a write is refused before anything runs
      select 1 as ok
    key: ok                  # identity of a row, so `dea diff` compares rows, not positions
    columns: [ok]            # optional: projection and order
    signals:
      - rows: "count == 0"   # aggregate rule over the section (name: count)
        message: "example returned no rows"
        severity: warn       # info | warn | error
      # - when: "age_h(started_at) > 6"     # per-row rule over the row's fields
      #   message: "job {{id}} has run for more than 6 h"
    render: table           # table | list
"""

CLI = """# Pack scaffold (kind: cli). Reference: docs/packs.md
pack: {name}
version: 1
kind: cli
requires: {{cli: kubectl}}     # executable that must be on PATH; the argv must be a recognised READ command of it
validated: fixtures
description: One sentence on what this pack watches.
params:
  namespaces: {{type: list, required: true, description: "namespaces to inspect (name them: no account-wide listing)"}}
sections:
  - name: example
    run: [kubectl, get, pods, -n, "{{{{item}}}}", -o, json]   # a list, never a shell string; {{{{item}}}} = each for_each value
    for_each: namespaces
    item_as: namespace                                      # the value is added to each row under this name
    select: "items[?status.phase != 'Running' && status.phase != 'Succeeded']"   # JMESPath over the JSON the command prints
    fields: {{name: metadata.name, phase: status.phase}}      # output field -> JMESPath per element
    columns: [namespace, name, phase]
    key: [namespace, name]
    signals:
      - rows: "count > 0"
        message: "{{count}} pod(s) not running"
"""

HTTP = """# Pack scaffold (kind: http). Reference: docs/packs.md
pack: {name}
version: 1
kind: http                   # only GET (and read-only GraphQL `query`) is ever sent
validated: fixtures
description: One sentence on what this pack watches.
params: {{}}
sections:
  - name: example
    get: /v1/things?limit=50          # relative to the source's base_url
    # graphql: "query {{ things {{ id status }} }}"   # instead of get: (never `mutation` or `subscription`)
    select: items                     # JMESPath over the JSON response
    fields: {{id: id, status: status}}
    columns: [id, status]
    key: id
    signals:
      - when: "status != 'ok'"
        message: "thing {{id}} is {{status}}"
"""

TEMPLATES = {"sql": SQL, "cli": CLI, "http": HTTP}


def scaffold(name: str, kind: str) -> str:
    return TEMPLATES[kind].format(name=name)
