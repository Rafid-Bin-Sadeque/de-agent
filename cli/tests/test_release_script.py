"""tools/release.sh v2: orphan first release, then fast-forward releases on top of origin/main; leak greps cover the new tree,
the new commit's diff and the WHOLE public history. Runs the real script against a scratch repo and a scratch bare remote."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "release.sh"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
USERS = "/Us" + "ers/someone/proj"  # a leak pattern, built from fragments so this file does not match its own greps

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("perl") is None, reason="needs git and perl")


def git(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {p.stderr}")
    return p.stdout.strip()


def bgit(bare, *args):
    return subprocess.run(["git", f"--git-dir={bare}", *args], capture_output=True, text=True).stdout.strip()


def release(repo, *args, env=None):
    return subprocess.run(["bash", str(repo / "tools" / "release.sh"), "acme", "de-agent", "--skip-checks", *args], cwd=repo,
                          capture_output=True, text=True, env={**os.environ, **GIT_ENV, "DEA_DENYLIST_FILE": "", **(env or {})})


def commit_all(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def set_version(repo, v):
    p = repo / "cli" / "pyproject.toml"
    p.write_text(f'[project]\nname = "x"\nversion = "{v}"\nhomepage = "https://github.com/OWNER/de-agent"\n')


@pytest.fixture
def scratch(tmp_path):
    repo = tmp_path / "work"
    (repo / "tools").mkdir(parents=True)
    (repo / "cli").mkdir()
    (repo / "docs").mkdir()
    shutil.copy(SCRIPT, repo / "tools" / "release.sh")
    git(repo, "init", "-q", "-b", "main")
    set_version(repo, "0.1.0")
    (repo / "README.md").write_text("see github.com/OWNER/de-agent and git@github.com:OWNER/de-agent.git\n")
    (repo / "docs" / "a.md").write_text("docs\n")
    commit_all(repo, "work 1: private history that must never be published")
    bare = tmp_path / "public.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    return repo, bare


def test_first_release_is_an_orphan_and_the_second_fast_forwards_on_top(scratch):
    repo, bare = scratch
    r1 = release(repo)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert "base: none (orphan" in r1.stdout and "tree 0, new-commit diff 0, whole history 0" in r1.stdout
    assert git(repo, "rev-list", "--parents", "-n1", "release/v0.1.0").split() == [git(repo, "rev-parse", "release/v0.1.0")]  # no parent
    assert git(repo, "rev-list", "--count", "release/v0.1.0") == "1"
    assert git(repo, "rev-parse", "v0.1.0") == git(repo, "rev-parse", "release/v0.1.0")
    assert "acme/de-agent" in git(repo, "show", "release/v0.1.0:README.md") and "OWNER" not in git(repo, "show", "release/v0.1.0:README.md")
    assert git(repo, "log", "-1", "--format=%an", "release/v0.1.0") == "de-agent maintainers"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(repo, "branch", "--list", "tmp-release-*") == ""
    git(repo, "push", "-q", "origin", "release/v0.1.0:main", "--tags")
    first = git(repo, "rev-parse", "release/v0.1.0")

    # the private history moves on; second release
    set_version(repo, "0.2.0")
    (repo / "docs" / "b.md").write_text("new doc\n")
    commit_all(repo, "work 2")
    r2 = release(repo)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "fast-forward" in r2.stdout and "tree 0, new-commit diff 0, whole history 0" in r2.stdout
    second = git(repo, "rev-parse", "release/v0.2.0")
    assert git(repo, "rev-list", "--parents", "-n1", second).split() == [second, first]  # parent = origin/main, i.e. the first release
    assert git(repo, "rev-list", "--count", "release/v0.2.0") == "2"
    assert git(repo, "log", "--format=%s", "release/v0.2.0") == "de-agent v0.2.0\nde-agent v0.1.0"
    assert git(repo, "rev-parse", "v0.2.0") == second
    assert "work 1" not in git(repo, "log", "--format=%s", "release/v0.2.0")  # private history stays private
    git(repo, "push", "origin", "release/v0.2.0:main", "--tags")  # no --force: a fast-forward
    assert bgit(bare, "rev-list", "--count", "main") == "2"
    assert bgit(bare, "rev-list", "--merges", "--count", "main") == "0"
    assert bgit(bare, "tag", "--list") == "v0.1.0\nv0.2.0"


def test_base_none_keeps_the_orphan_path_even_when_origin_main_exists(scratch):
    repo, _ = scratch
    assert release(repo).returncode == 0
    git(repo, "push", "-q", "origin", "release/v0.1.0:main")
    set_version(repo, "0.2.0")
    commit_all(repo, "work 2")
    r = release(repo, "--base", "none")
    assert r.returncode == 0 and "base: none" in r.stdout
    assert git(repo, "rev-list", "--count", "release/v0.2.0") == "1"


def test_explicit_base_ref(scratch):
    repo, _ = scratch
    assert release(repo).returncode == 0
    base = git(repo, "rev-parse", "release/v0.1.0")
    set_version(repo, "0.2.0")
    commit_all(repo, "work 2")
    r = release(repo, "--base", "release/v0.1.0")
    assert r.returncode == 0 and git(repo, "rev-parse", "release/v0.2.0^") == base
    bad = release(repo, "--base", "no-such-ref")
    assert bad.returncode == 1 and "does not exist" in bad.stderr


def test_version_flag_must_match_pyproject(scratch):
    repo, _ = scratch
    r = release(repo, "--version", "9.9.9")
    assert r.returncode == 1 and "does not match" in r.stderr
    ok = release(repo, "--version", "0.1.0")
    assert ok.returncode == 0 and git(repo, "rev-parse", "--verify", "v0.1.0")
    assert release(repo, "--version", "not-a-version").returncode == 1


def test_dirty_tree_and_no_changes_are_refused(scratch):
    repo, _ = scratch
    (repo / "dirty.txt").write_text("x")
    r = release(repo)
    assert r.returncode == 1 and "dirty" in r.stderr
    (repo / "dirty.txt").unlink()
    assert release(repo).returncode == 0
    git(repo, "push", "-q", "origin", "release/v0.1.0:main", "--tags")
    again = release(repo)  # same tree as origin/main and tag v0.1.0 already public
    assert again.returncode == 1 and ("nothing to release" in again.stderr or "already part of the public history" in again.stderr)


def test_a_leak_in_the_new_tree_aborts_and_leaves_no_branch_or_tag(scratch):
    repo, _ = scratch
    (repo / "docs" / "leak.md").write_text(f"see {USERS}\n")
    commit_all(repo, "leak")
    r = release(repo)
    assert r.returncode == 1 and "ABORT" in r.stderr
    assert git(repo, "branch", "--list", "release/*") == "" and git(repo, "tag", "--list") == ""
    assert "tree 1" in r.stdout or "new-commit diff 1" in r.stdout


def test_a_leak_only_in_the_public_history_still_aborts(scratch):
    """The latest tree is clean, but an EARLIER public commit contains the leak: the greps cover the whole public history."""
    repo, bare = scratch
    # publish a commit that leaks (as if an earlier release had gone out wrong)
    (repo / "docs" / "leak.md").write_text(f"see {USERS}\n")
    commit_all(repo, "leak")
    assert release(repo).returncode == 1
    r = subprocess.run(["bash", str(repo / "tools" / "release.sh"), "acme", "de-agent", "--skip-checks", "--base", "none"], cwd=repo,
                       capture_output=True, text=True, env={**os.environ, **GIT_ENV, "DEA_DENYLIST_FILE": ""})
    assert r.returncode == 1  # orphan of a tree that still has the leak
    # now fix the tree and publish a clean orphan, then simulate a bad earlier public commit by pushing a leaking commit by hand
    (repo / "docs" / "leak.md").unlink()
    commit_all(repo, "fix")
    assert release(repo).returncode == 0
    git(repo, "push", "-q", "origin", "release/v0.1.0:main")
    git(repo, "checkout", "-q", "-b", "scratch-pub", "origin/main")
    (repo / "docs" / "oops.md").write_text(f"{USERS}\n")
    commit_all(repo, "oops")
    git(repo, "rm", "-q", "docs/oops.md")
    commit_all(repo, "remove oops")
    git(repo, "push", "-q", "origin", "scratch-pub:main")
    git(repo, "checkout", "-q", "main")
    set_version(repo, "0.2.0")
    (repo / "docs" / "b.md").write_text("b\n")
    commit_all(repo, "work 2")
    r = release(repo)
    assert r.returncode == 1 and "ABORT" in r.stderr
    import re

    line = next(ln for ln in r.stdout.splitlines() if ln.strip().startswith("'/Us" + "ers/'"))
    assert re.search(r"tree 0, new-commit diff 0, whole history [1-9]", line), line  # the tree and the new diff are clean; only the history is not
    assert git(repo, "branch", "--list", "release/v0.2.0") == "" and git(repo, "tag", "--list", "v0.2.0") == ""


def test_denylist_file_is_applied_to_tree_diff_and_history(scratch, tmp_path):
    repo, _ = scratch
    deny = tmp_path / "deny.txt"
    deny.write_text("\nsecret-codename\n")
    (repo / "docs" / "c.md").write_text("project Secret-Codename\n")
    commit_all(repo, "c")
    r = release(repo, env={"DEA_DENYLIST_FILE": str(deny)})
    assert r.returncode == 1 and "deny-list" in r.stdout and "tree 1" in r.stdout
    assert "Secret-Codename" not in r.stdout + r.stderr  # counts and file:line only, never the match


def test_skills_are_filled_and_an_unfilled_owner_link_refuses_with_main_left_clean(scratch):
    repo, _ = scratch
    (repo / "skills" / "s").mkdir(parents=True)
    (repo / "skills" / "s" / "SKILL.md").write_text("docs at https://github.com/OWNER/de-agent/blob/main/x.md\n")
    commit_all(repo, "skill")
    r = release(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "github.com/acme/de-agent" in git(repo, "show", "release/v0.1.0:skills/s/SKILL.md")
    (repo / "misc").mkdir()
    (repo / "misc" / "note.md").write_text("see https://github.com/OWNER/other\n")
    set_version(repo, "0.1.1")
    commit_all(repo, "unfilled")
    r = release(repo, "--version", "0.1.1")
    assert r.returncode != 0 and "unfilled OWNER placeholders" in r.stderr and "misc/note.md" in r.stderr
    assert git(repo, "status", "--porcelain") == "" and git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
