# de-agent — original design plan

> Original design plan, kept for the rationale. It was written before the build, so details have since moved on: for what the
> project does today read the [README](../../README.md) and [docs/architecture.md](../architecture.md). The milestone specs
> are in [specs/](specs/).

> Working name: `de-agent`. Before the first public push, check that the name is free on GitHub and PyPI.
> Built by a data engineer, from patterns used day to day on a private multi-project data platform.
> **Hard rule:** zero employer or production data anywhere in this repo. No hostnames, endpoints, schema or table names,
> people, incidents or numbers. Every example comes from the bundled demo stack.

## 1. Thesis

Coding agents read repos well. Data engineering truth mostly lives elsewhere, in running systems: warehouse
state, orchestrator runs, CDC lag, lake jobs. An agent that can't see them guesses, and guessing is where "AI slop" comes from.

de-agent gives a Claude Code agent:
1. a **generated, timestamped map of the live platform** (read-only collectors), so it starts every task knowing
   the current state, without spending turns exploring;
2. a **standard hub** of hand-written knowledge (purpose, conventions, gotchas) kept small via progressive disclosure;
3. a **plan → approve → execute** workflow with model tiering (the main model thinks, cheaper agents fetch and execute);
4. **enforced guardrails**: the agent cannot write to a database outside an approved plan, and cannot leak credentials;
5. **proof**: an eval suite against a demo platform with seeded faults, scoring each task with and without the plugin.

The user types the task ("why is the orders mart stale?"). The system brings the context.

## 2. Principles (each one is tested, not just stated)

| # | Principle | Enforced by |
|---|---|---|
| P1 | Facts are generated or verified, never remembered | collectors + `generated:` stamps; skills tag every claim V (verified) or I (inferred) |
| P2 | Freshness matches volatility: config authored, state generated, numbers queried live | hub layout + `dea lint` staleness rules |
| P3 | Read-only unless an approved plan says otherwise | `sql_guard` PreToolUse hook (deny holds even in bypass mode) + read-only DB roles (documented as the primary control) |
| P4 | Secrets never enter the transcript | `secret_guard` PreToolUse hook |
| P5 | Small always-loaded context (map ≤ 1,500 tokens) | `dea lint` budget rule; SessionStart injects map + due items only |
| P6 | Thinking and doing are separate | agents: runner (read-only), executor (approved plans only) |
| P7 | Claims of usefulness are measured | `claude plugin eval` with/without ablation, results committed and in README |

## 3. Repo layout (one repo = marketplace + plugin + CLI)

```
de-agent/
├── .claude-plugin/
│   ├── plugin.json              name, version, description, license Apache-2.0, repository, keywords
│   └── marketplace.json         owner + one entry {name: de-agent, source: "./"}
├── agents/
│   ├── runner.md                read-only lookups (SQL, CLIs, logs) → compact answer + evidence; model: sonnet
│   ├── executor.md              runs an approved plan file exactly; stops on any deviation; model: sonnet
│   └── explorer.md              code/fact search across repos; model: sonnet
├── skills/
│   ├── hub-init/                scaffold a hub + de-agent.yaml for a team (interactive)
│   ├── hub-sync/                after work: update profiles/status/integrations from the session
│   ├── onboard-project/         add a repo to the hub (profile, map row, lint clean)
│   ├── plan/                    plan → approval → plan file → executor handoff
│   ├── pipeline-check/          "did last night's run work?" across orchestrator + warehouse + DQ
│   ├── incident-triage/         symptom → snapshot diff → runs → queries → root cause + fix plan
│   ├── audit/                   capacity/health audit; engine packs: references/{postgres,redshift}.md
│   ├── backfill/                safe backfill plan: scope, idempotency, batches, validation, rollback
│   ├── schema-change/           migration plan: impact via lineage (dbt) + consumers + rollout
│   └── new-pipeline/            build to the team's standard (hub conventions page), shadow-test first
├── hooks/
│   └── hooks.json               SessionStart, PreToolUse(Bash|mcp__.*), PostToolUse(Bash), Stop
├── scripts/                     hook entrypoints (python3, stdlib + vendored sqlglot only)
│   ├── session_context.py       inject map.md + overdue/due-soon status rows + stale-snapshot warning
│   ├── sql_guard.py             classify SQL in psql/MCP calls; deny writes outside an approved plan
│   ├── secret_guard.py          deny inline secrets / secret-file reads
│   ├── query_log.py             append every DB call to .dea/logs/queries.jsonl
│   └── stop_check.py            once per session: remind hub-sync if project files changed and status.md didn't
├── cli/                         Python package `de-agent` → `dea` command (pipx install de-agent)
│   ├── pyproject.toml
│   └── src/de_agent/
│       ├── cli.py               dea snapshot | diff | lint | doctor | init | guard-test
│       ├── config.py            de-agent.yaml schema (pydantic), secret refs only (env:, pgpass, aws profile)
│       ├── runner.py            exec targets: local | ssh:<host> (ship collector, stream JSON back)
│       ├── render.py            JSON → platform/*.md (deterministic, snapshot-tested)
│       ├── diff.py              snapshot N vs N-1 → "what changed" (new failures, lag jumps, config drift)
│       ├── lint.py              budget, links, staleness, overdue status, contradictions
│       ├── sqlclass.py          sqlglot-based statement classifier (shared with sql_guard)
│       └── collectors/
│           ├── base.py          Collector protocol: name, requires, collect() -> dict, schema version
│           ├── postgres.py      size, bloat/dead tuples, long queries, locks, replication slots, index health
│           ├── redshift.py      cluster/WLM/svv_table_info hygiene, alerts, commit queue (sys_* views, timeouts)
│           ├── airflow.py       REST API v1/v2: DAGs, last N runs, failures, import errors, schedules, datasets
│           ├── dbt.py           manifest.json + run_results.json: models, tests, lineage, last results
│           ├── aws_dms.py       instances → tasks → endpoints, settings, CDC lag, swap/memory (CloudWatch)
│           ├── aws_glue.py      catalog dbs/tables, jobs + last runs
│           ├── aws_s3.py        configured prefixes: object count/size/newest object age
│           ├── aws_athena.py    workgroups, recent query failures, scanned bytes
│           ├── aws_firehose.py  delivery streams, destination, delivery freshness/errors
│           └── cloudwatch.py    shared metric helper (per-day/weekday/weekend/pinned-hours summaries)
├── templates/hub/               map.md, status.md, conventions.md, projects/_template.md, integrations/_template.md
├── demo/                        docker compose: source Postgres → Airflow → warehouse Postgres + dbt + SeaweedFS (S3 stand-in)
│   ├── docker-compose.yml
│   ├── seed/                    small realistic e-commerce dataset (generated, no real data)
│   └── faults/                  01_schema_drift … 06_lock_blocker: inject.sh + revert.sh + EXPECTED.md
├── evals/                       claude plugin eval suite: one case per fault + guardrail cases
├── tests/                       pytest: collectors (fixtures/moto), sqlclass, guards, render snapshots, lint
├── docs/                        architecture, safety-model, writing-a-collector, adoption-guide, design/PLAN.md
├── .github/workflows/           ci.yml (validate + unit + demo integration), evals.yml (manual/nightly)
├── LICENSE  README.md  SECURITY.md  CONTRIBUTING.md  CHANGELOG.md
```

## 4. Key designs

**4.1 Config (`de-agent.yaml`, in the team's hub).** It holds secret references only, never values. If a value appears where a reference is expected, `dea doctor` fails.
```yaml
hub: .                          # map.md, status.md, projects/ … live here
platform_dir: platform          # generated output
sources:
  - {name: wh, type: postgres, dsn: "env:WH_DSN", exec: local}
  - {name: dwh, type: redshift, host: …, user: readonly_agent, password: pgpass, exec: "ssh:bastion"}
  - {name: af, type: airflow, base_url: "env:AIRFLOW_URL", auth: "env:AIRFLOW_TOKEN"}
  - {name: dbt, type: dbt, target_dir: ../analytics/target}
  - {name: aws, type: aws, profile: readonly, region: …, services: [dms, glue, s3, athena, firehose]}
guard: {mode: strict, write_roles_forbidden: true}   # strict = deny, ask = prompt the user
```
`exec: ssh:<host>` sends the collector over ssh to a box that holds the credentials and gets JSON back. This pattern was proven
in production: no local credentials, nothing to install remotely beyond python3.

**4.2 Snapshots and diff.** `dea snapshot` writes `platform/<source>.md` plus `.dea/snapshots/<ts>.json` (30 are kept).
`dea diff` prints what changed since the previous snapshot: new task failures, CDC lag jumps, a resized instance, a new DAG,
a dbt test that started failing. incident-triage starts here, because "what changed?" is the first question in any incident.

**4.3 SQL guard (defence in depth; read-only DB roles stay the primary control).**
- Extracts SQL from `psql -c`, `psql -f <file>`, psql heredocs, `bq query`, `aws redshift-data execute-statement`,
  `aws athena start-query-execution`, and MCP tools whose names match `query|sql|execute`.
- Classifies each statement with sqlglot (postgres/redshift dialects). A statement that can't be parsed counts as a write (fail closed).
- READ = SELECT/WITH-select/SHOW/EXPLAIN (EXPLAIN ANALYZE counts as a write, because it executes the query)/`\d` meta.
- WRITE = everything else, including CALL, VACUUM, ANALYZE, COPY, UNLOAD, GRANT, SET ROLE and `SELECT … INTO`.
- A WRITE is allowed only when `agent_type == de-agent:executor` AND `.dea/plans/<id>.approved` exists AND the
  statement's normalized hash is listed in that plan. Otherwise: deny (strict) or ask.
- Limits are documented honestly: SQL inside arbitrary Python scripts isn't seen, which is why read-only roles come first.
- `dea guard-test` runs the classifier corpus. It is also a CI test with more than 150 cases.

**4.4 Plans.** The `plan` skill writes `.dea/plans/<id>.md`: goal, exact steps, exact SQL blocks, validation, rollback, and
approved outward actions. The user approves with `dea approve <id>`, which writes `<id>.approved` with statement hashes. The executor gets the
file path, not a paraphrase. Every write therefore traces back to a reviewed plan.

**4.5 Session context.** The SessionStart hook injects at most 6,000 characters: map.md, status rows due within 2 days or overdue,
and a warning for any snapshot older than `max_age` (default 24 h). No CLAUDE.md edits are needed, so it works in any repo.

**4.6 Lint (`dea lint`, also run in the team's CI).** Rules: map.md ≤ 1,500 tokens; no broken `[[links]]`; profile
`updated:` not older than its repo's last commit (profile frontmatter `repo:`); no snapshot older than max_age; status rows
past their date without being marked done; a project in map.md with no profile, or the reverse.

**4.7 Evals (`claude plugin eval`, ablation with/without the plugin).** One case per demo fault plus guardrail cases.
Graders: regex on the root cause, `tool_used` (runner was used), an LLM grader for the quality of the fix plan, and a guardrail check
(`.dea/logs/queries.jsonl` contains no write). Published in the README with pass rate, cost and turns for each arm.

## 5. Milestones (each one ends green in CI; nothing is published until M5)

| M | Scope | Acceptance |
|---|---|---|
| **M0** | Skeleton: plugin.json + marketplace.json, LICENSE/README stub, CLI package skeleton, `sqlclass` + `sql_guard` + `secret_guard` + tests, CI (plugin validate + ruff + pytest) | `claude plugin validate . --strict` passes; guard corpus ≥ 150 cases green; `claude --plugin-dir .` loads with no plugin_errors |
| M1 | Config + runner (local/ssh) + render + diff + lint + Postgres, Airflow and dbt collectors; demo stack (compose, seed, 6 faults); hub templates; SessionStart + query_log hooks | `docker compose up` + `dea snapshot` renders all three; each fault visible in `dea diff`; lint clean on templates |
| M2 | Agents + skills: hub-init, hub-sync, onboard-project, plan (+ `dea approve`), pipeline-check, incident-triage, audit (postgres pack); stop_check hook | walkthrough on the demo: init → triage fault 01 → plan → approve → executor fix → re-snapshot clean |
| M3 | Redshift collector + redshift audit pack (generalized, no production data); AWS collectors (DMS, Glue, S3, Athena, Firehose, CloudWatch), tested with moto + recorded fixtures | ≥ 85% line coverage in collectors; rendered fixture snapshots reviewed |
| M4 | Eval suite (6 fault cases + 4 guardrail cases + 2 planning cases), evals workflow, baseline comparison | with-plugin pass rate clearly above baseline; zero guardrail violations across 3 runs per case |
| M5 | Docs (architecture diagram, safety model, collector guide, adoption guide), 60-s demo GIF, CHANGELOG, v0.1.0 tag, PyPI publish, dogfood on a private platform (write-up with no data) | maintainer's public push + announcement (maintainer's own outward actions) |

## 6. Division of work

- Opus (main session): this plan, each milestone's detailed spec, review of every executor report, all design calls.
- `executor` (Sonnet): builds each milestone from its spec. `runner`/`Explore`: lookups.
- Maintainer: approves each milestone spec; all outward actions (GitHub repo creation, push, PyPI, announcement).

## 7. Open decisions

1. Final name (availability check before M5).
2. Supported Python: 3.10+ (proposed).
3. Demo AWS: SeaweedFS (S3 stand-in) stands in for S3 only. DMS/Glue/Athena/Firehose are covered by fixtures (LocalStack's free tier lacks them).
4. Should `dea` also run as an MCP server later (v0.2) for non-Claude-Code agents? Collectors are already plain CLI/JSON.

---

## M0 spec (for executor)

Working dir `de-agent/` (already holds `docs/design/PLAN.md`). Approved actions: create files, `git init`, local
commits, create a local venv at `cli/.venv`, `pip install` into it. **Not approved:** creating a remote, pushing, or publishing.

1. `git init`; `.gitignore`: `.venv/ __pycache__/ .dea/ *.egg-info dist/ .pytest_cache/ .ruff_cache/ platform/`.
2. `.claude-plugin/plugin.json`:
   `{"name":"de-agent","version":"0.0.1","description":"Ground-truth context, guardrails and workflows for data engineering with Claude Code","license":"Apache-2.0","keywords":["data-engineering","airflow","dbt","redshift","postgres","aws","guardrails"],"repository":"https://github.com/Rafid-Bin-Sadeque/de-agent"}`
   `.claude-plugin/marketplace.json`: `{"name":"de-agent","owner":{"name":"de-agent maintainers"},"plugins":[{"name":"de-agent","source":"./","description":"Ground-truth context, guardrails and workflows for data engineering"}]}`
3. `LICENSE` = Apache-2.0 full text. `README.md` = title, thesis paragraph (§1), "status: pre-alpha", install placeholder.
   `SECURITY.md`: report privately; scope = the guard hooks. `CONTRIBUTING.md`: dev setup + tests.
4. `cli/pyproject.toml`: hatchling; name `de-agent`; requires-python `>=3.10`; deps `sqlglot>=25`, `pydantic>=2`,
   `pyyaml`, `typer`; optional `[aws]=boto3`, `[pg]=psycopg[binary]>=3`, `[dev]=pytest, ruff, moto[all]`;
   script `dea = de_agent.cli:app`.
5. `cli/src/de_agent/sqlclass.py`: `classify(sql: str, dialect: str = "postgres") -> list[Stmt]`. `Stmt` = dataclass
   (kind: "read"|"write", type: str, text: str, hash: str = sha256 of sqlglot-normalized SQL, reason: str).
   Rules from §4.3. Parse failure → kind "write", reason "unparseable (fail closed)". Multi-statement input is split.
   Also handle `\d`, `\dt`, `\l`, `\x` and similar psql meta-commands as read.
6. `cli/src/de_agent/extract.py`: `extract_sql(command: str) -> list[tuple[str, str]]` (source, sql) for: psql `-c`
   (repeatable, quoted forms), `psql -f path` (read the file if it exists; missing → "unreadable" → write), heredoc
   `psql … <<'EOF' … EOF`, `bq query '…'`, `aws redshift-data execute-statement --sql '…'`,
   `aws athena start-query-execution --query-string '…'`. Return [] when no DB client appears.
7. `scripts/sql_guard.py` (stdlib only + `sys.path` insert of `${CLAUDE_PLUGIN_ROOT}/cli/src`; if sqlglot can't be imported →
   deny every DB command with reason "de-agent CLI not installed: pipx install de-agent"). Reads the hook JSON on stdin.
   For Bash it uses `extract_sql(tool_input.command)`. For `mcp__.*(query|sql|execute).*` it uses tool_input fields
   `sql|query|statement`. Any write → if `agent_type == "de-agent:executor"` and the hash is in
   `$CLAUDE_PROJECT_DIR/.dea/plans/*.approved` (JSON `{"hashes":[…]}`) → allow; else print to stdout
   `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"|"ask","permissionDecisionReason":"de-agent: <type> is a write; writes need an approved plan (dea approve). Statement: <first 120 chars>"}}`.
   Mode comes from `$CLAUDE_PROJECT_DIR/de-agent.yaml guard.mode` (default strict). The guard must never crash: on an
   internal error it denies with reason "de-agent guard error: <msg>" (fail closed).
8. `scripts/secret_guard.py`: deny Bash commands matching inline `PGPASSWORD=`, `MYSQL_PWD=`, `AWS_SECRET_ACCESS_KEY=`,
   `password=` inside a DSN/URI (`postgres(ql)?://[^:]+:[^@]+@`), and reads (`cat|less|head|tail|grep|sed|awk`) of
   `~/.pgpass`, `.env`, `*.pem`, `~/.aws/credentials`. Deny `Read` tool calls on the same paths (matcher `Read`).
9. `hooks/hooks.json`:
   `{"description":"de-agent guardrails","hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"python3","args":["${CLAUDE_PLUGIN_ROOT}/scripts/secret_guard.py"],"timeout":10},{"type":"command","command":"python3","args":["${CLAUDE_PLUGIN_ROOT}/scripts/sql_guard.py"],"timeout":15}]},{"matcher":"Read","hooks":[{"type":"command","command":"python3","args":["${CLAUDE_PLUGIN_ROOT}/scripts/secret_guard.py"],"timeout":10}]},{"matcher":"mcp__.*","hooks":[{"type":"command","command":"python3","args":["${CLAUDE_PLUGIN_ROOT}/scripts/sql_guard.py"],"timeout":15}]}]}}`
10. Tests (`cli/tests/`):
    - `test_sqlclass.py`: a corpus file `tests/corpus/sql_cases.yaml` with **≥ 150** cases (sql, dialect, expected kind/type).
      It must cover: CTE+DELETE, `SELECT … INTO`, EXPLAIN vs EXPLAIN ANALYZE, CALL, VACUUM/ANALYZE, COPY/UNLOAD,
      GRANT/REVOKE, SET ROLE, multi-statement with a write hidden in the 2nd statement, comments hiding a write,
      Redshift-specific syntax, unparseable garbage.
    - `test_extract.py`: ≥ 30 command-line forms, including heredoc, `-f`, quoting, `bq`/`redshift-data`/`athena`.
    - `test_hooks.py`: run both scripts as subprocesses with fixture hook JSON (Bash read, Bash write, executor+approved
      hash, executor+unapproved, MCP query write, secret inline, `.pgpass` read, malformed stdin → deny).
11. `.github/workflows/ci.yml`: ubuntu, Python 3.10 and 3.12 matrix: `pip install -e cli[dev]`, `ruff check`, `pytest -q`;
    a job that installs Claude Code (`npm i -g @anthropic-ai/claude-code`) and runs `claude plugin validate . --strict`.
12. Validate locally:
    `cd de-agent && python3 -m venv cli/.venv && cli/.venv/bin/pip install -e 'cli[dev]' && cli/.venv/bin/ruff check cli scripts && cli/.venv/bin/pytest -q cli/tests`
    then `claude plugin validate . --strict`. Then a smoke test:
    `echo '{"tool_name":"Bash","tool_input":{"command":"psql -c \"delete from t\""},"cwd":"."}' | python3 scripts/sql_guard.py`
    → deny JSON; same with `select 1` → no output and exit 0.
13. Commit: `git add -A && git commit -m "M0: plugin skeleton, SQL/secret guardrails, CI"` (attribution trailer per session rules).
14. Report: files created, test counts and results, validate output, any case where the classifier needed a judgment call.
