import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "scripts" / "_vendor", ROOT / "cli" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _isolate_hub_env(monkeypatch, tmp_path_factory):
    """No test may pick up a real hub from the developer's environment."""
    monkeypatch.delenv("DEA_HUB", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    """Nothing in the suite may reach a real AWS account: fake static creds, no profile, no instance metadata."""
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_ROLE_ARN",
              "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/dea-test-aws-config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/dea-test-aws-credentials")


@pytest.fixture
def example_hub(tmp_path):
    """A lint-clean hub scaffolded by dea init --with-example."""
    from de_agent.initcmd import init_hub

    hub = tmp_path / "hub"
    init_hub(hub, "Demo", with_example=True)
    return hub


HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)


FIXTURES = Path(__file__).resolve().parent / "fixtures"

PIPELINES_PAGE = """---
owner: data-eng
domain: acme
updated: 2026-10-01
---
# pipelines

Human text that a scan must never touch.
Second line with trailing spaces{pad}
""".replace("{pad}", "   ")


@pytest.fixture
def scan_hub(tmp_path):
    """A hub + copies of the fixture repos laid out as siblings: hub/, airflow_repo/, warehouse_sql/, analytics/."""
    import shutil

    base = tmp_path / "ws"
    hub = base / "hub"
    (hub / "projects").mkdir(parents=True)
    shutil.copytree(FIXTURES / "airflow_repo", base / "airflow_repo")
    shutil.copytree(FIXTURES / "warehouse_sql", base / "warehouse_sql")
    shutil.copytree(FIXTURES / "dbt_proj", base / "analytics")
    (hub / "de-agent.yaml").write_text(
        "version: 1\n"
        "scans:\n"
        "  - {name: pipelines, type: airflow, path: ../airflow_repo, dag_folders: [dags], util_packages: [common, utils],"
        ' dataset_tables: ["warehouse://{schema}/{table}"]}\n'
        "  - {name: warehouse_sql, type: sql, path: ../warehouse_sql, project: etl}\n"
        "  - {name: analytics, type: dbt, path: ../analytics, target_dir: target}\n")
    (hub / "projects" / "pipelines.md").write_text(PIPELINES_PAGE)
    (hub / "projects" / "analytics.md").write_text(
        "---\nowner: a\ndomain: acme\nupdated: 2026-10-01\n---\n# analytics\n\nHuman text.\n")
    return hub
