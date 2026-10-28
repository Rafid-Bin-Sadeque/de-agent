import os
import stat

import pytest
from de_agent.config import ConfigError, parse_config, resolve_secret

BASE = """
version: 1
hub: {name: "Acme data platform", map_budget_tokens: 1500, snapshot_max_age_hours: 24, retention: 30}
sources:
  - name: warehouse
    type: postgres
    exec: local
    timeout_seconds: 120
    conn: {host: localhost, port: 5432, dbname: shop, user: readonly_agent, password: "pgpass", sslmode: prefer}
    options: {schemas: [public, analytics], top_tables: 25}
guard: {mode: strict}
"""


def test_valid_config():
    cfg = parse_config(BASE)
    assert cfg.hub.name == "Acme data platform"
    s = cfg.source("warehouse")
    assert s.type == "postgres" and s.conn.password == "pgpass" and s.options["top_tables"] == 25
    assert cfg.guard.mode == "strict"


def test_defaults_and_empty():
    cfg = parse_config("")
    assert cfg.sources == [] and cfg.hub.retention == 30


def test_secret_literal_rejected():
    with pytest.raises(ConfigError, match="secret literal in config; use env:/pgpass/file:"):
        parse_config(BASE.replace('password: "pgpass"', 'password: "hunter2"'))


def test_secret_literal_in_unknown_section_rejected():
    with pytest.raises(ConfigError, match="secret literal"):
        parse_config("version: 1\nextra:\n  api_key: abc123\n")


def test_env_and_none_refs_ok():
    assert parse_config(BASE.replace('"pgpass"', '"env:WH_PW"')).sources[0].conn.password == "env:WH_PW"
    assert parse_config(BASE.replace('"pgpass"', '"none"')).sources[0].conn.password == "none"


def test_file_ref_with_open_permissions_rejected(tmp_path):
    f = tmp_path / "pw"
    f.write_text("x")
    f.chmod(0o644)
    with pytest.raises(ConfigError, match="0600"):
        parse_config(BASE.replace('"pgpass"', f'"file:{f}"'))


def test_file_ref_0600_ok_and_resolves(tmp_path):
    f = tmp_path / "pw"
    f.write_text("s3cret\n")
    f.chmod(0o600)
    cfg = parse_config(BASE.replace('"pgpass"', f'"file:{f}"'))
    assert resolve_secret(cfg.sources[0].conn.password) == "s3cret"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_file_ref_must_be_absolute():
    with pytest.raises(ConfigError, match="absolute"):
        parse_config(BASE.replace('"pgpass"', '"file:relative/pw"'))


def test_unknown_type_lists_registered():
    with pytest.raises(ConfigError, match=r"unknown source type 'mysql'.*registered types: airflow, aws, dbt, postgres, redshift"):
        parse_config(BASE.replace("type: postgres", "type: mysql"))


def test_ssh_with_env_secret_rejected():
    bad = BASE.replace("exec: local", "exec: ssh:bastion").replace('"pgpass"', '"env:WH_PW"')
    with pytest.raises(ConfigError, match="requires password: pgpass"):
        parse_config(bad)


def test_ssh_with_pgpass_ok():
    cfg = parse_config(BASE.replace("exec: local", "exec: ssh:bastion"))
    assert cfg.sources[0].exec == "ssh:bastion"


def test_ssh_host_cannot_look_like_an_option():
    with pytest.raises(ConfigError, match="exec must be"):
        parse_config(BASE.replace("exec: local", "exec: ssh:-oProxyCommand=x"))


def test_extra_keys_forbidden():
    with pytest.raises(ConfigError):
        parse_config(BASE + "unexpected: 1\n")


def test_duplicate_source_names_rejected():
    second = "  - name: warehouse\n    type: postgres\n    conn: {dbname: d, user: u}\n"
    with pytest.raises(ConfigError, match="duplicate source names"):
        parse_config(BASE.replace("guard:", second + "guard:"))


def test_bad_source_name_and_options():
    with pytest.raises(ConfigError, match="source name"):
        parse_config(BASE.replace("name: warehouse", "name: Warehouse!"))
    with pytest.raises(ConfigError, match="unknown postgres options"):
        parse_config(BASE.replace("top_tables: 25", "top_tables: 25, bogus: 1"))


def test_resolve_secret_env(monkeypatch):
    monkeypatch.setenv("WH_PW", "abc")
    assert resolve_secret("env:WH_PW") == "abc"
    assert resolve_secret("pgpass") is None and resolve_secret("none") is None
    monkeypatch.delenv("WH_PW")
    with pytest.raises(ConfigError, match="not set"):
        resolve_secret("env:WH_PW")


def test_timeout_default_is_type_specific():
    cfg = parse_config("sources:\n  - {name: dwh, type: redshift, data_api: {cluster_id: c, database: d, db_user: u}, aws: {profile: p, region: r}}\n  - {name: pg, type: postgres, conn: {dbname: d, user: u}}\n"
                       "  - {name: dwh2, type: redshift, timeout_seconds: 90, data_api: {cluster_id: c, database: d, db_user: u}, aws: {profile: p, region: r}}\n")
    assert [s.timeout_seconds for s in cfg.sources] == [600, 120, 90]


def test_env_secret_falls_back_to_the_hub_dotenv(tmp_path, monkeypatch):
    from de_agent.config import ConfigError, resolve_secret

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "de-agent.yaml").write_text("version: 1\n")
    (hub / ".env").write_text("# c\nA_ONE=plain\nexport A_TWO=\"quoted value\"\nA_THREE='single' \nA_FOUR=x # note\n")
    monkeypatch.chdir(hub)
    monkeypatch.delenv("A_ONE", raising=False)
    assert resolve_secret("env:A_ONE") == "plain"
    assert resolve_secret("env:A_TWO") == "quoted value"
    assert resolve_secret("env:A_THREE") == "single"
    assert resolve_secret("env:A_FOUR") == "x"
    monkeypatch.setenv("A_ONE", "from-process")  # the process environment wins
    assert resolve_secret("env:A_ONE") == "from-process"
    with pytest.raises(ConfigError) as ei:
        resolve_secret("env:NOT_THERE")
    assert "NOT_THERE" in str(ei.value) and "plain" not in str(ei.value)


def test_env_secret_without_dotenv_is_still_an_error(tmp_path, monkeypatch):
    from de_agent.config import ConfigError, resolve_secret

    (tmp_path / "de-agent.yaml").write_text("version: 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NOPE_X", raising=False)
    with pytest.raises(ConfigError):
        resolve_secret("env:NOPE_X")
