"""The example collector and scanner in the docs actually run."""
import re
from pathlib import Path

from de_agent import render as R
from de_agent.collectors import REGISTRY
from de_agent.collectors.base import RunContext
from de_agent.config import SourceConfig

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _python_blocks(name):
    return re.findall(r"```python\n(.*?)```", (DOCS / name).read_text(), re.S)


def test_example_collector_runs_and_renders(tmp_path, monkeypatch):
    ns = {}
    exec(_python_blocks("writing-a-collector.md")[1], ns)  # block 0 is the protocol sketch
    status = tmp_path / "status.json"
    status.write_text('{"queues": [{"name": "billing", "depth": 1200, "oldest_age_s": 5}, {"name": "mail", "depth": 3}]}')
    monkeypatch.setitem(REGISTRY, "jobqueue", ns["JobQueueCollector"]())
    monkeypatch.setitem(R.SIGNALS, "jobqueue", ns["jobqueue_signals"])
    monkeypatch.setitem(R.BODIES, "jobqueue", ns["jobqueue_body"])
    src = SourceConfig(name="jobs", type="jobqueue", options={"status_file": str(status)})
    out = REGISTRY["jobqueue"].collect(src, RunContext(timeout_seconds=10))
    result = {"source": "jobs", "type": "jobqueue", "schema_version": 1, "collected_at": "2026-01-01T00:00:00Z",
              "duration_s": 0.0, **out}
    page = R.render(result)
    assert "queue billing depth 1200" in page and "| mail | 3 | 0 |" in page
    assert page == R.render(result)


def test_example_scanner_functions_run():
    blocks = _python_blocks("writing-a-scanner.md")
    ns = {}
    exec(blocks[1], ns)  # block 0 is the contract sketch
    one = ns["scan_makefile_text"]("all: build\n\nbuild:\n\tcc -o app main.c\n", "Makefile", "postgres")
    assert [t["name"] for t in one["targets"]] == ["all", "build"]
    assert ns["assemble"](type("S", (), {"name": "mk", "project_name": "mk"})(), {"Makefile": one}, "rev1")["stats"]["targets"] == 2
