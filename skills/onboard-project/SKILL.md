---
name: onboard-project
description: Add one repo to an existing de-agent hub. Configures the scan, writes the profile page with the human (purpose, owners, gotchas), rescans, and proposes an integration page for each new cross-project edge. Use when the user says "onboard this repo" or "add this project to the hub".
---

# onboard-project

## When to use
A repo exists that is not yet in `de-agent.yaml` `scans:` (or has no `projects/<name>.md` profile).

## Inputs
- Repo path and project name (slug).
- Scan type: `airflow`, `dbt` or `sql`, plus options (`dag_folders`, `util_packages`, `dataset_tables`, `target_dir`, `globs`).
- From the human, 3 to 5 short answers for the profile body: purpose, owners, the top gotchas, how it is
  deployed, who to ask. Ask these as questions; do not invent them from code.

## Steps
1. Add the `scans:` entry to `de-agent.yaml` (paths relative to the hub). Show the diff first.
2. `dea scan --name <scan>`. Read the stats it prints (files parsed, DAGs/models, unresolved references).
3. Profile `projects/<name>.md`: the generated header block is owned by `dea scan`; do not edit between the
   `<!-- dea:generated:start/end -->` markers. Write the human body below it from the human's answers.
   Frontmatter needs `owner`, `domain`, `updated: <today>`, and `repo:` when the repo is a git checkout.
4. Add the project to `map.md` (one line) or the domain page. Keep it within budget.
5. `dea scan` again so the header, catalog and edges include the new project.
6. For each NEW cross-project edge (`dea edges <table>` for the tables the project reads or writes that other
   projects also touch), draft an integration page `integrations/<a>--<b>.md` containing ONLY what the edge
   cannot express: contract, timing, gotchas. Propose it to the human; write it only after they accept.
7. `dea lint` must exit 0.

## Output
- Diff of every file changed or proposed, the scan stats, the list of proposed integration pages, and `dea lint`.

## Never
- Never touch generated blocks, `catalog/` or `platform/` by hand.
- Never fill the profile body with guesses: if the human did not say it, leave it out or mark it `TODO(human)`.
- Never write secrets into the hub. Never run `dea apply` or `dea approve`.
