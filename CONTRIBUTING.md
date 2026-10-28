# Contributing

Thanks for helping. Two rules first: **never include real company data** (hostnames, schemas, tables, people, incidents, numbers) in code,
tests, fixtures or docs, and **never weaken a guard** to make a test pass. Everything here uses the fictional Acme Shop demo.

## Dev setup

```bash
python3 -m venv cli/.venv
cli/.venv/bin/pip install -e 'cli[dev]'
```

Python 3.10+ for the CLI. The hook scripts in `scripts/` must also run on 3.9 (they run under the system `python3`); keep them stdlib-only
apart from the vendored `sqlglot` in `scripts/_vendor`. To verify: `DEA_HOOK_PYTHON=/usr/bin/python3 cli/.venv/bin/pytest cli/tests/test_hooks.py`.

**Editing the plugin with the plugin loaded.** de-agent's `self_guard` hook stops any agent from editing the plugin's own `hooks/`,
`scripts/`, `agents/`, `skills/` and `.claude-plugin/` (so an agent can't switch off its own guardrails). When you are developing de-agent
itself and want Claude to help, start Claude from a shell where you set the escape hatch yourself:

```bash
DEA_ALLOW_PLUGIN_EDITS=1 claude --plugin-dir .
```

An agent cannot set this for itself: hooks read the environment of the shell that started Claude. Don't set it in normal use.

## Test tiers

| tier | command | needs | when |
|---|---|---|---|
| lint | `cli/.venv/bin/ruff check cli scripts evals` | | always |
| unit | `cli/.venv/bin/pytest -q cli/tests` | | always; fast, no network, no docker |
| integration | included in the default run, skipped without docker | docker (`postgres:16`) | collectors and `dea query` against a real database |
| demo e2e | `make demo-up && DEA_DEMO=1 cli/.venv/bin/pytest -m demo` | docker | snapshot, diff, plan, apply against the demo stack |
| walkthrough | `DEA_DEMO=1 cli/.venv/bin/pytest -m walkthrough` | docker, `claude` CLI, API spend | scripted `claude -p` flow on fault 01; run locally, not in CI |
| perf | `cli/.venv/bin/pytest -m perf` | | scan timings on 1,000 synthetic DAGs; excluded by default |
| evals | `cli/.venv/bin/python evals/run.py --out evals/results/<date>` | docker, `claude` CLI, API spend | with and without the plugin; see [evals/README.md](evals/README.md) |

Also run `claude plugin validate . --strict` and, after changing a config model, `dea config schema --markdown -o docs/configuration.md`
and `dea config schema -o docs/de-agent.schema.json` (a test fails when they drift). Regenerate golden renders on purpose with
`DEA_UPDATE_GOLDEN=1` and read the diff.

## Common changes

- **SQL classifier behaviour** needs a case in `cli/src/de_agent/data/sql_cases.yaml` (it is also what `dea guard-test` runs).
- **A new collector or scanner**: [docs/writing-a-collector.md](docs/writing-a-collector.md), [docs/writing-a-scanner.md](docs/writing-a-scanner.md).
- **A skill or agent**: edit `skills/<name>/SKILL.md` or `agents/<name>.md`; keep descriptions trigger-phrased, add a "Never" section, and
  run the plugin validator.
- **A hook change** gets tests in `cli/tests/test_hooks.py` (allowed and denied cases, and malformed input must deny).

## How to add a fault (demo and evals)

1. Create `demo/faults/NN_name/` with `inject.sh`, `revert.sh` (idempotent; source `_lib.sh` for the helpers) and `EXPECTED.md`: the
   root cause, the keywords an answer must contain, and the evidence a correct answer cites.
2. Add the case file `evals/cases/fNN_name.yaml` (id, prompt in a user's words, `setup.inject`, `teardown.revert`, keyword groups, the
   unsafe actions to watch for) and a unit test for any new grader rule in `cli/tests/test_evals.py`.
3. Check by hand: `make fault-inject N=NN`, ask the agent, `make fault-revert N=NN`. Try the baseline too (a fault both conditions
   solve tells you nothing).
4. Run the case a few times (`evals/run.py --only FNN --runs-override 3 --dry-run` to preview). Never edit a verdict by hand: fix the
   grader, then `evals/regrade.py` from the saved transcripts and say so in the report.

## Pull requests

Small and focused, with tests. Describe the behaviour change and how you checked it. By contributing you agree your contribution is
licensed under Apache-2.0. Be kind: see the [Code of Conduct](CODE_OF_CONDUCT.md). Vulnerabilities: [SECURITY.md](SECURITY.md).
