"""Close-out fixes in plan hashing (exact patch bytes) and shell_destructive recursion."""
from types import SimpleNamespace

import pytest
from de_agent import stepexec as sx
from de_agent.plans import blocks_covered

PATCH_CLEAN = "--- a/x.sql\n+++ b/x.sql\n@@ -1 +1 @@\n-select 1\n+select 2\n"
PATCH_WS = "--- a/x.sql\n+++ b/x.sql\n@@ -1 +1 @@\n-select 1  \n+select 2\n"


def blk(patch):
    return SimpleNamespace(kind="diff", repo="r", sql=patch)


def test_hash_covers_exact_bytes_including_trailing_whitespace():
    assert sx.diff_hash("r", PATCH_CLEAN) != sx.diff_hash("r", PATCH_WS)
    assert sx.diff_hash("r", PATCH_WS) != sx.legacy_diff_hash("r", PATCH_WS)


def test_hash_crlf_equals_lf_only():
    assert sx.diff_hash("r", PATCH_CLEAN.replace("\n", "\r\n")) == sx.diff_hash("r", PATCH_CLEAN)


def test_hash_is_of_what_git_apply_receives():
    import hashlib

    assert sx.diff_hash("r", PATCH_WS) == hashlib.sha256(f"diff\nrepo=r\n{sx.exact_patch(PATCH_WS)}".encode()).hexdigest()


def test_legacy_candidate_only_when_texts_identical():
    assert sx.legacy_diff_hash("r", PATCH_CLEAN) in sx.diff_hash_candidates("r", PATCH_CLEAN)
    assert sx.legacy_diff_hash("r", PATCH_WS) not in sx.diff_hash_candidates("r", PATCH_WS)


def test_old_approval_still_verifies_for_identical_text():
    old = {sx.legacy_diff_hash("r", PATCH_CLEAN)}
    assert blocks_covered([blk(PATCH_CLEAN)], old) == []


def test_old_approval_rejected_when_whitespace_diverges():
    old = {sx.legacy_diff_hash("r", PATCH_WS)}
    assert blocks_covered([blk(PATCH_WS)], old)


def test_new_approval_verifies_and_other_patch_does_not():
    ok = {sx.diff_hash("r", PATCH_WS)}
    assert blocks_covered([blk(PATCH_WS)], ok) == []
    assert blocks_covered([blk(PATCH_CLEAN)], ok)


@pytest.mark.parametrize("cmd", [
    "bash -c 'rm -rf /data/x'",
    "sh -c \"dbt run --full-refresh\"",
    "zsh -c 'airflow dags delete x'",
    "bash -lc 'aws s3api delete-bucket --bucket b'",
    "env FOO=1 bash -c 'rm x'",
    "env -S 'rm -rf x'",
    "bash -c \"bash -c 'rm x'\"",
])
def test_shell_destructive_recurses(cmd):
    assert sx.shell_destructive(cmd)


@pytest.mark.parametrize("cmd", ["bash -c 'ls -la'", "env FOO=1 dbt run", "sh -c 'echo hi'", "dbt run --select m"])
def test_shell_destructive_leaves_safe_commands(cmd):
    assert sx.shell_destructive(cmd) is None
