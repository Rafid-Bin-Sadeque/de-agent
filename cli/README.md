# de-agent

The `dea` command-line tool of **de-agent**: ground-truth context, enforced guardrails and workflows for data engineering with
[Claude Code](https://claude.com/claude-code).

`dea` collects live platform state (Postgres, Redshift, Airflow, dbt, AWS) read-only into timestamped markdown, scans your repos into table-level
lineage, searches both (`dea find`), and runs the plan, approve, apply workflow that keeps an agent from writing to a database without a human's
signature. The Claude Code plugin (skills, agents, hooks) lives in the same repository and is installed separately.

```bash
uv tool install 'de-agent[pg,aws]'   # or: pipx install 'de-agent[pg,aws]'
# extras: pg = psycopg driver, aws = boto3 (AWS collectors, Redshift Data API); install only what you use
dea init --name "My platform" && dea doctor
```

Documentation, the plugin, the safety model and the eval results: <https://github.com/Rafid-Bin-Sadeque/de-agent>. Apache-2.0.
