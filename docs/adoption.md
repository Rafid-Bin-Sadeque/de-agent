# Adoption guide

From one warehouse and one Airflow to a few hundred projects, without the hub turning into a second job.

## Day 1: one warehouse, one Airflow (about an hour)

1. Install the CLI and the plugin ([README](../README.md#install)).
2. Create a hub folder (its own git repo is a good home) and scaffold it:
   ```bash
   mkdir platform-hub && cd platform-hub && git init
   dea init --name "Acme data platform"
   ```
   Or ask Claude: "set up de-agent" (the `hub-init` skill asks for the folders and sources, then runs the commands below).
3. Edit `de-agent.yaml`. Start with two sources and one scan. Secrets are references, never values
   (full reference: [configuration.md](configuration.md)):
   ```yaml
   sources:
     - name: warehouse
       type: postgres            # or redshift
       conn: {host: wh.example.internal, dbname: analytics, user: readonly_agent, password: pgpass}
     - name: airflow
       type: airflow
       options: {base_url: "https://airflow.example.internal", auth: basic, user: agent_ro, password: "env:AF_PASSWORD"}
   scans:
     - {name: dags, type: airflow, path: ../acme-airflow, dag_folders: [dags]}
   ```
   Use a read-only database role. The guards are defence in depth; the role is the primary control ([SAFETY.md](SAFETY.md)).
4. `dea doctor` (checks Python, the config, every source, `psql`, the FTS index, the hook Python and the vendored SQL parser),
   then `dea scan` and `dea snapshot`. Read `platform/warehouse.md`: the signals at the top are what the agent will see.
5. Write `map.md` (one line per project) and one project page. Run `dea lint`. Start a Claude Code session in the hub and ask a
   real question ("why is the orders mart stale?").

## Week 1: more repos, real profiles, status

- Add a scan per repo (`onboard-project` skill: it configures the scan, writes the profile with you, rescans and proposes an
  integration page for each new cross-project edge). Airflow, dbt (point `target_dir` at the folder with `manifest.json`) and plain SQL are supported.
- Fill project pages with what only a human knows: purpose, owners, gotchas. The header block is generated (`<!-- dea:generated -->`); do not edit it.
- Add `status.md` rows for dated checks and work in progress; rows that are due are injected at session start.
- Add `conventions.md`: how things are built here, in a few lines.
- Run `dea snapshot` daily (below) so the agent never works from stale state; a stale snapshot is flagged at session start.
- Use `hub-sync` at the end of a work session: it proposes one diff for the hub, you accept or edit it.

## Scaling to hundreds of projects

- **Domains.** When `map.md` goes over its token budget (`dea lint` says so), split it by domain: `domains/<domain>.md` lists the
  projects of that domain (60 lines max), and `map.md` lists the domains. Lookups never walk the tree; `dea find` jumps to the page.
- **Ownership.** Put `owner`, `domain`, `repo` and `tags` in each profile's frontmatter, and use `CODEOWNERS` so each domain's
  pages are reviewed by the team that owns them.
- **Scheduled snapshots.** Generated layers should refresh without a person. Any scheduler works, because `dea snapshot` and `dea scan`
  are plain commands that exit non-zero when a source fails:
  ```
  # cron, daily
  15 6 * * *  cd /srv/platform-hub && dea snapshot && dea scan && git add -A && git commit -qm "refresh" && git push
  ```
  The same two commands fit a CI schedule or an Airflow `BashOperator`. Run them where the credentials live; a source can use
  `exec: ssh:<host>` to collect from another host (then use `password: pgpass` on that host).
- **Lint in CI.** `dea lint --json` on every pull request to the hub blocks broken links, blown budgets, bad frontmatter,
  stale pages and secret-looking text. Exit code 1 on any error.
- **Speed.** Sources are collected concurrently with a timeout each; scans re-parse only changed files. Heavy Redshift sections are reused
  for a few hours (`--deep` recomputes).

## Team rollout

- **One shared hub repo**, reviewed like code. Each engineer clones it and points the plugin at it (`DEA_HUB`, or `hub:` in
  `~/.config/de-agent/config.yaml`). Generated files may be committed by the scheduled job or rebuilt locally; pick one.
- **Credentials stay personal.** `de-agent.yaml` holds references (`env:`, `pgpass`, `file:`), so the same file works for everyone with
  their own credentials. Give the agent a read-only role.
- **Per-person approval key.** `dea approve` signs with `$XDG_CONFIG_HOME/de-agent/approve.key` (created `0600` on first use, never shared).
  A plan approved by one person verifies only against that person's key, which is the point: the approver is on record in the `.approved` file.
  Decide whether plans are committed (audit trail) or kept local, and keep write-capable sources (`writable: true`) to the few who run plans.
- **Start in `ask` mode if the team is nervous.** `guard: {mode: ask}` prompts instead of denying a write; move to `strict` once the
  plan workflow is familiar.
- **Measure.** The eval suite under `evals/` runs the same cases with and without the plugin on the fictional demo; copy a few cases for
  your own platform to see what the agent gets wrong before you trust it.
