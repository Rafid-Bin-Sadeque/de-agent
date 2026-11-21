"""v0.3.3: DMS lineage from Terraform-managed tasks (data sources, var IDs, renames, snapshot fallback, drift)."""
import json

from de_agent.scanners.terraform import _apply_dms_rename, assemble, scan_terraform_text

# TF shaped like the real repo: data source target, var task id, computed mappings
FIXTURE_VAR_TF = '''
variable "task_id" {
  type    = string
  default = "test-api-backend-secrets-demo"
}
variable "target_ep_id" {
  type = string
}
data "aws_dms_endpoint" "target" {
  endpoint_id   = var.target_ep_id
  endpoint_type = "target"
  engine_name   = "postgres"
}
resource "aws_dms_replication_task" "demo" {
  replication_task_id = var.task_id
  target_endpoint_arn = data.aws_dms_endpoint.target.endpoint_arn
  table_mappings      = jsonencode(local.table_mappings)
}
'''

# TF with literal JSON + schema rename
FIXTURE_RENAME_TF = '''
data "aws_dms_endpoint" "tgt" {
  endpoint_id   = "tgt-pg"
  endpoint_type = "target"
  engine_name   = "postgres"
}
resource "aws_dms_replication_task" "task" {
  replication_task_id = "my-task"
  target_endpoint_arn = data.aws_dms_endpoint.tgt.endpoint_arn
  table_mappings = <<EOT
{"rules": [
  {"rule-type": "selection", "rule-id": "1", "rule-action": "include",
   "schema-name": "api_backend", "table-name": "users"},
  {"rule-type": "selection", "rule-id": "2", "rule-action": "include",
   "schema-name": "api_backend", "table-name": "accounts"},
  {"rule-type": "transformation", "rule-id": "3", "rule-action": "rename",
   "rule-target": "schema", "schema-name": "api_backend", "value": "test_api_backend"}
]}
EOT
}
'''


def _snap_fixture(tmp_path, rows, ts="2026-11-20T00:00:00Z"):
    snap_dir = tmp_path / ".dea" / "snapshots" / "2026-11-20T00"
    snap_dir.mkdir(parents=True)
    (snap_dir / "aws_main.json").write_text(json.dumps({
        "collected_at": ts, "data": {"dms_table_mappings": rows}
    }))
    return tmp_path


def _tables():
    return [{"task": "test-api-backend-secrets-demo", "schema": "api_backend", "table": t,
             "target_schema": "test_api_backend", "target_table": t}
            for t in ["users", "accounts", "orders", "payments", "trades",
                      "positions", "sessions", "logs", "events"]]


class _Scan:
    name = "infra"
    project_name = "test"
    dialect = "postgres"


def _run(tf_text, hub=None):
    pf = {"main.tf": scan_terraform_text(tf_text, "main.tf", "postgres")}
    return assemble(_Scan(), pf, "rev", hub=hub)


def test_data_source_not_unresolved():
    """data.aws_dms_endpoint.target is resolved; not in unresolved_refs."""
    r = _run(FIXTURE_VAR_TF)
    assert not any("target_endpoint_arn" in u["name"] for u in r["unresolved_refs"])


def test_var_task_id_resolves():
    """replication_task_id = var.task_id -> dms:test-api-backend-secrets-demo."""
    r = _run(FIXTURE_VAR_TF)
    node = next(n for n in r["nodes"] if n["type"] == "aws_dms_replication_task")
    assert node["id"] == "dms:test-api-backend-secrets-demo"


def test_snapshot_fallback_9_pairs(tmp_path):
    """computed table_mappings + snapshot -> 9 pairs with renamed target schema, via live-snapshot."""
    hub = _snap_fixture(tmp_path, _tables())
    r = _run(FIXTURE_VAR_TF, hub=hub)
    node = next(n for n in r["nodes"] if n["type"] == "aws_dms_replication_task")
    assert len(node["pairs"]) == 9
    assert all(p["target"].startswith("test_api_backend.") for p in node["pairs"])
    assert all("live-snapshot:" in p.get("via", "") for p in node["pairs"])
    assert node.get("snapshot_fallback") is True


def test_rename_applied_to_static_pairs():
    """Static literal mapping + schema rename -> target uses renamed schema."""
    r = _run(FIXTURE_RENAME_TF)
    node = next(n for n in r["nodes"] if n["type"] == "aws_dms_replication_task")
    targets = {p["target"] for p in node["pairs"]}
    assert targets == {"test_api_backend.users", "test_api_backend.accounts"}
    assert not node.get("snapshot_fallback")


def test_drift_warning(tmp_path):
    """Static pairs and snapshot disagree -> drift warning, no failure."""
    # snapshot for my-task has 3 pairs; static mapping has 2 -> drift
    drift_rows = [{"task": "my-task", "schema": "api_backend", "table": t,
                   "target_schema": "test_api_backend", "target_table": t}
                  for t in ["users", "accounts", "orders"]]
    hub = _snap_fixture(tmp_path, drift_rows)
    r = _run(FIXTURE_RENAME_TF, hub=hub)
    assert any("mapping drift" in w and "my-task" in w for w in r.get("warnings", []))


def test_no_snapshot_no_pairs():
    """No snapshot + computed mappings -> empty pairs (existing behaviour)."""
    r = _run(FIXTURE_VAR_TF, hub=None)
    node = next(n for n in r["nodes"] if n["type"] == "aws_dms_replication_task")
    assert node["pairs"] == []


def test_apply_dms_rename_schema():
    assert _apply_dms_rename("api_backend", "users",
        [{"type": "transformation", "action": "rename", "target": "schema",
          "schema": "api_backend", "value": "test_api_backend"}]) == ("test_api_backend", "users")


def test_apply_dms_rename_no_match():
    assert _apply_dms_rename("other", "tbl",
        [{"type": "transformation", "action": "rename", "target": "schema",
          "schema": "api_backend", "value": "test_api_backend"}]) == ("other", "tbl")
