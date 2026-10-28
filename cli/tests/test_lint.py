import subprocess
from datetime import date, datetime, timezone

from de_agent.lint import run_lint

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def lint(hub, **kw):
    return run_lint(hub, today=TODAY, now=NOW, **kw)


def rules(findings, rule=None):
    return [f for f in findings if rule is None or f.rule == rule]


def test_example_hub_is_clean(example_hub):
    assert lint(example_hub) == []


def test_scaffold_without_example_is_clean(tmp_path):
    from de_agent.initcmd import init_hub

    init_hub(tmp_path / "h", "Demo")
    assert lint(tmp_path / "h") == []


def test_map_budget(example_hub):
    assert not rules(lint(example_hub), "map-budget")
    (example_hub / "map.md").write_text((example_hub / "map.md").read_text() + "\n" + "word " * 1600)
    f = rules(lint(example_hub), "map-budget")
    assert len(f) == 1 and f[0].level == "error" and "budget 1500" in f[0].message


def test_map_budget_respects_config(example_hub):
    cfg = example_hub / "de-agent.yaml"
    cfg.write_text(cfg.read_text().replace("map_budget_tokens: 1500", "map_budget_tokens: 20"))
    assert rules(lint(example_hub), "map-budget")


def test_map_missing(tmp_path):
    (tmp_path / "de-agent.yaml").write_text("version: 1\n")
    assert rules(lint(tmp_path), "map-budget")[0].message == "map.md is missing"


def test_page_budget(example_hub):
    prof = example_hub / "projects" / "orders_etl.md"
    prof.write_text(prof.read_text() + "\nx\n" * 150)
    f = rules(lint(example_hub), "page-budget")
    assert [x.path for x in f] == ["projects/orders_etl.md"]
    dom = example_hub / "domains" / "sales.md"
    dom.write_text("# Sales\n" + "line\n" * 61)
    (example_hub / "conventions.md").write_text("# C\n" + "line\n" * 151)
    paths = sorted(x.path for x in rules(lint(example_hub), "page-budget"))
    assert paths == ["conventions.md", "domains/sales.md", "projects/orders_etl.md"]


def test_page_budget_boundary(example_hub):
    (example_hub / "domains" / "sales.md").write_text("# Sales\n" + "line\n" * 59)  # exactly 60 lines
    assert not rules(lint(example_hub), "page-budget")


def test_broken_link(example_hub):
    prof = example_hub / "projects" / "orders_etl.md"
    prof.write_text(prof.read_text() + "\nSee [[does_not_exist]] and [[orders_etl--warehouse|the seam]].\n")
    f = rules(lint(example_hub), "broken-link")
    assert len(f) == 1 and "does_not_exist" in f[0].message


def test_links_in_code_and_templates_ignored(example_hub):
    prof = example_hub / "projects" / "orders_etl.md"
    prof.write_text(prof.read_text() + "\nUse `[[x]]` for links.\n```\n[[also_not_a_link]]\n```\n")
    assert not rules(lint(example_hub), "broken-link")


def test_orphan_profile_not_listed(example_hub):
    (example_hub / "projects" / "stray.md").write_text(
        "---\nowner: a\ndomain: d\nupdated: 2026-10-01\n---\n# Stray\n")
    f = rules(lint(example_hub), "orphan-project")
    assert [x.path for x in f] == ["projects/stray.md"]


def test_orphan_listed_without_profile(example_hub):
    m = example_hub / "map.md"
    m.write_text(m.read_text().replace("| [[orders_etl]]", "| ghost_project | nothing | nowhere |\n| [[orders_etl]]"))
    f = rules(lint(example_hub), "orphan-project")
    assert len(f) == 1 and "ghost_project" in f[0].message


def test_profile_frontmatter(example_hub):
    prof = example_hub / "projects" / "orders_etl.md"
    prof.write_text("---\ndomain: sales\nupdated: yesterday\n---\n# X\n")
    msgs = " ".join(f.message for f in rules(lint(example_hub), "profile-frontmatter"))
    assert "owner" in msgs and "YYYY-MM-DD" in msgs
    prof.write_text("---\nowner: a\ndomain: sales\n---\n# X\n")
    assert "updated" in " ".join(f.message for f in rules(lint(example_hub), "profile-frontmatter"))


def _git_repo(path, commit_date):
    path.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date,
           "PATH": __import__("os").environ["PATH"], "HOME": str(path)}
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=path, check=True, env=env)


def test_profile_stale(example_hub, tmp_path):
    repo = tmp_path / "repo"
    _git_repo(repo, "2026-10-20T10:00:00+00:00")
    prof = example_hub / "projects" / "orders_etl.md"

    def set_updated(d):
        prof.write_text(f"---\nowner: a\ndomain: sales\nupdated: {d}\nrepo: {repo}\n---\n# X\nSee [[orders_etl--warehouse]].\n")

    set_updated("2026-10-01")   # commit is 19 days after updated -> stale
    f = rules(lint(example_hub), "profile-stale")
    assert len(f) == 1 and f[0].level == "warning"
    set_updated("2026-10-06")   # 14 days: not beyond the grace period
    assert not rules(lint(example_hub), "profile-stale")


def test_profile_stale_ignores_missing_repo(example_hub):
    prof = example_hub / "projects" / "orders_etl.md"
    prof.write_text("---\nowner: a\ndomain: s\nupdated: 2026-01-01\nrepo: /nonexistent/path\n---\n# X\n")
    assert not rules(lint(example_hub), "profile-stale")


def test_snapshot_stale(example_hub):
    p = example_hub / "platform"
    (p / "wh.md").write_text("---\nsource: wh\ntype: postgres\ngenerated: 2026-10-26T10:00:00Z\nschema_version: 1\n---\n# wh\n")
    (p / "fresh.md").write_text("---\nsource: fresh\ngenerated: 2026-10-27T11:00:00Z\n---\n# fresh\n")
    (p / "index.md").write_text("---\ntype: platform-index\n---\n# index\n")
    f = rules(lint(example_hub), "snapshot-stale")
    assert [x.path for x in f] == ["platform/wh.md"] and f[0].level == "warning"


def test_status_overdue(example_hub):
    s = example_hub / "status.md"
    s.write_text(s.read_text().replace(
        "| Due | Check | State |\n|---|---|---|\n",
        "| Due | Check | State |\n|---|---|---|\n| 2026-10-20 | verify nightly load | todo |\n"
        "| 2026-10-21 | old check | done |\n| 2026-11-30 | future check | todo |\n"))
    f = rules(lint(example_hub), "status-overdue")
    assert len(f) == 1 and "verify nightly load" in f[0].message and f[0].level == "warning"


def test_secret_in_hub(example_hub):
    assert not rules(lint(example_hub), "secret-in-hub")
    (example_hub / "conventions.md").write_text("# C\nconnect with postgres://admin:hunter2@db.example/shop\n")
    (example_hub / "projects" / "notes.txt").write_text("run PGPASSWORD=hunter2 psql\n")
    (example_hub / "aws.md").write_text("AWS_SECRET_ACCESS_KEY=" + "A" * 40 + "\n")
    paths = sorted(f.path for f in rules(lint(example_hub), "secret-in-hub"))
    assert paths == ["aws.md", "conventions.md", "projects/notes.txt"]
    assert all(f.level == "error" for f in rules(lint(example_hub), "secret-in-hub"))


def test_secret_placeholders_allowed(example_hub):
    (example_hub / "conventions.md").write_text(
        "# C\nuse postgres://user:${PW}@host/db and PGPASSWORD=$PW and password=<redacted>\n")
    assert not rules(lint(example_hub), "secret-in-hub")


def test_config_with_literal_secret_is_an_error(example_hub):
    cfg = example_hub / "de-agent.yaml"
    cfg.write_text("version: 1\nsources:\n  - name: wh\n    type: postgres\n"
                   "    conn: {dbname: d, user: u, password: hunter2}\n")
    assert rules(lint(example_hub), "config-invalid")


def test_demo_hub_is_lint_clean():
    from pathlib import Path

    from de_agent.lint import run_lint

    demo = Path(__file__).resolve().parents[2] / "demo"
    errors = [f for f in run_lint(demo) if f.level == "error"]
    assert errors == [], errors
