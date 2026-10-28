"""Record Airflow REST responses + trimmed dbt artifacts from the running demo stack as test fixtures.

usage: python demo/tools/record_fixtures.py <name> [--dbt-only | --airflow-only]    (e.g. healthy | failing)
Recorded for the committed fixtures: airflow healthy + failing (fault 01 state), dbt healthy + failing (fault 05 state).
Needs the stack up and demo/.env values in the environment (DEMO_AF_PASSWORD). Demo data only.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli" / "src"))

from de_agent.collectors import _airflow_core as core  # noqa: E402

OUT = ROOT / "cli" / "tests" / "fixtures"


def key(path, params):
    return path + ("?" + urlencode(sorted((str(k), str(v)) for k, v in (params or []))) if params else "")


def record_airflow(name, base_url):
    cfg = {"base_url": base_url, "auth": "basic", "user": "agent_ro"}
    real = core.make_urllib_get(base_url, core.auth_header_for(cfg, os.environ["DEMO_AF_PASSWORD"]))
    cassette = {}

    def recording(path, params=None, accept="application/json"):
        status, body = real(path, params, accept)
        cassette[key(path, params)] = {"status": status, "body": body}
        return status, body

    now = datetime.now(timezone.utc)
    res = core.gather({"base_url": base_url}, recording, now=now)
    doc = {"now": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "cassette": cassette}
    d = OUT / "airflow_2_10"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"airflow/{name}: {len(cassette)} responses, errors={res['errors']}")


def trim_manifest(m):
    keep_node = ("unique_id", "resource_type", "name", "path", "original_file_path", "attached_node", "depends_on")
    nodes = {}
    for uid, n in (m.get("nodes") or {}).items():
        t = {k: n[k] for k in keep_node if k in n}
        t["config"] = {"materialized": (n.get("config") or {}).get("materialized")}
        nodes[uid] = t
    return {"metadata": {k: m["metadata"].get(k) for k in
                         ("dbt_schema_version", "dbt_version", "generated_at", "project_name", "adapter_type")},
            "nodes": nodes, "exposures": m.get("exposures") or {}}


def record_dbt(name, target):
    d = OUT / "dbt" / name
    d.mkdir(parents=True, exist_ok=True)
    m = json.loads((target / "manifest.json").read_text())
    (d / "manifest.json").write_text(json.dumps(trim_manifest(m), indent=1, sort_keys=True) + "\n")
    for f in ("run_results.json", "sources.json"):
        doc = json.loads((target / f).read_text())
        for r in doc.get("results", []):
            for k in ("adapter_response", "compiled_code", "timing", "thread_id", "relation_name", "batch_results"):
                r.pop(k, None)
        (d / f).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"dbt/{name}: recorded")


if __name__ == "__main__":
    name = sys.argv[1]
    if "--dbt-only" not in sys.argv:
        record_airflow(name, f"http://localhost:{os.environ.get('DEMO_AF_PORT', '8085')}")
    if "--airflow-only" not in sys.argv:
        record_dbt(name, ROOT / "demo" / "dbt" / "acme" / "target")
