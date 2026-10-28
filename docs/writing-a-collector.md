# Writing a collector

A collector reads one kind of live system and returns plain data. `dea snapshot` runs every configured source concurrently, renders the
result to `platform/<source>.md`, and `dea diff` compares it with the previous snapshot. Adding a system (say Snowflake) is one module,
one renderer, a registry line, and fixtures.

## The protocol

```python
class Collector(Protocol):
    type: str                          # the `type:` value in de-agent.yaml
    schema_version: int                # bump when the JSON shape changes
    list_keys: dict[str, list[str]]    # section -> identity fields of its rows, so `dea diff` matches rows, not positions
    volatile_fields: set[str]          # fields `dea diff` ignores (ages, timestamps, free text)

    def collect(self, source: SourceConfig, ctx: RunContext) -> dict:
        """Return {"data": {section: ...}, "errors": [str]}. A failing section goes to errors; the rest still returns."""
```

Optional: `validate_options(options)` and `validate_source(source)` (raise `ValueError`; run when the config loads),
`ping(source, ctx) -> str` (used by `dea doctor`), `windowed_sections` and `ranking_sections` (sections whose rows age out of a window or
form a top-N, so entering or leaving them is not reported as a change). `ctx.remaining()` is the time left of the source's budget;
`CollectorError` is the way to say "refused or failed" (its message must never contain a secret).

## A minimal example

A collector for a hypothetical job queue that publishes a JSON status file:

```python
import json
from pathlib import Path

from de_agent.collectors.base import CollectorError, RunContext


class JobQueueCollector:
    type = "jobqueue"
    schema_version = 1
    list_keys = {"queues": ["name"]}
    volatile_fields = {"oldest_age_s"}

    @staticmethod
    def validate_options(options):
        if "status_file" not in options:
            raise ValueError("options.status_file is required")

    def collect(self, source, ctx: RunContext):
        try:
            doc = json.loads(Path(source.options["status_file"]).read_text())
        except (OSError, ValueError) as ex:
            raise CollectorError(f"cannot read status file: {type(ex).__name__}") from None
        queues = [{"name": q["name"], "depth": int(q["depth"]), "oldest_age_s": int(q.get("oldest_age_s", 0))}
                  for q in doc.get("queues", [])]
        return {"data": {"queues": queues}, "errors": []}


def jobqueue_signals(data):
    return [f"queue {q['name']} depth {q['depth']}" for q in data.get("queues", []) if q["depth"] > 1000]


def jobqueue_body(data):
    rows = ["| queue | depth | oldest (s) |", "|---|---|---|"]
    rows += [f"| {q['name']} | {q['depth']} | {q['oldest_age_s']} |" for q in sorted(data.get("queues", []), key=lambda q: q["name"])]
    return ["## Queues", "", *rows, ""]
```

Register it in three places:

1. `cli/src/de_agent/collectors/__init__.py`: add `JobQueueCollector.type: JobQueueCollector()` to `REGISTRY`.
2. `cli/src/de_agent/render.py`: add the signal function to `SIGNALS` and the body function to `BODIES`.
3. `docs/collectors.md` (or a new page): document the options, the sections and each signal's threshold.

Then `type: jobqueue` is valid in `de-agent.yaml` and `dea snapshot` writes `platform/<source>.md`, with a "Signals" list first.

## Rules

- **Read-only, always.** Use `SELECT`s, `Describe*` / `List*` / `Get*` API calls and file reads. Database SQL goes through
  `ctx.query(source, sql)`: it classifies the statement first (fail closed) and runs it in a read-only transaction with a timeout. AWS calls
  go through the guarded session (a botocore `before-call` handler refuses anything that is not a read; see [SAFETY.md](SAFETY.md)).
- **No secrets in output.** Never put a connection string, token or key into `data`, `errors` or an exception message. Resolve secrets at
  run time with `resolve_secret`, and let `redact` clean error text.
- **Degrade by section.** One failing query must not blank the page: record it in `errors`, keep the other sections.
- **Respect the budget.** Check `ctx.remaining()` between steps; cap lists (top N) and record the cap in `data["meta"]` so the page can say so.
- **Deterministic data.** No timestamps of "now" inside sections you want diffed (put them in `volatile_fields`), stable ordering, no random ids.
- **Signals are facts with thresholds**, written as one line each ("queue billing depth 1200"), so the agent can rank hypotheses from the top of the page.

## Fixtures and golden renders

Tests never touch a live system. Record or hand-write a fixture with fictional values (`cli/tests/fixtures/<type>_<case>.json`, the
collector result shape: `{"source", "type", "schema_version", "collected_at", "duration_s", "data", "errors"}`) and compare the render against a golden file:

```python
def test_golden(name):
    got = R.render(load(name))                         # cli/tests/test_render.py
    golden = HERE / "golden" / f"{name}.md"
    if os.environ.get("DEA_UPDATE_GOLDEN"):
        golden.write_text(got)                         # regenerate on purpose, then read the diff
    assert got == golden.read_text()
```

Also test: the same input renders to the same bytes, shuffled rows render identically, a failing section keeps the rest, and (for a
SQL source) that every query classifies as a read. Inject `query_fn` or a fake `aws_session` through `RunContext` instead of mocking modules.
For a database collector, an integration test against a real container (`pytest -m integration`) catches SQL that fixtures cannot.
