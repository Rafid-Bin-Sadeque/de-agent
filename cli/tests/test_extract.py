import pytest
from de_agent.extract import extract_sql
from de_agent.sqlclass import classify

CASES = [
    ("psql -c 'select 1'", [("psql", "select 1")]),
    ('psql -c "delete from t"', [("psql", "delete from t")]),
    ("psql --command 'select 2'", [("psql", "select 2")]),
    ("psql --command='select 3'", [("psql", "select 3")]),
    ("psql -c'select 4'", [("psql", "select 4")]),
    ("psql -Atc 'select 5'", [("psql", "select 5")]),
    ("psql -X -q -c 'select 6' -c 'select 7'", [("psql", "select 6"), ("psql", "select 7")]),
    ("psql -h localhost -U bob -d db -c 'update t set a = 1'", [("psql", "update t set a = 1")]),
    ("psql 'host=h dbname=d' -c 'select 8'", [("psql", "select 8")]),
    ("PGOPTIONS='-c statement_timeout=1' psql -c 'select 9'", [("psql", "select 9")]),
    ("psql -c \"select 'it''s'\"", [("psql", "select 'it''s'")]),
    ("psql -c 'select \"Col\" from t'", [("psql", 'select "Col" from t')]),
    ("psql -c 'select 1' && psql -c 'drop table t'", [("psql", "select 1"), ("psql", "drop table t")]),
    ("psql -c 'select 1'; psql -c 'truncate t'", [("psql", "select 1"), ("psql", "truncate t")]),
    ("psql -c 'select 1'\npsql -c 'delete from t'", [("psql", "select 1"), ("psql", "delete from t")]),
    ("psql -c 'select 1' \\\n  -c 'select 2'", [("psql", "select 1"), ("psql", "select 2")]),
    ("sudo -u postgres psql -c 'vacuum'", [("psql", "vacuum")]),
    ("docker exec -i pg psql -U u -c 'delete from t'", [("psql", "delete from t")]),
    ("ssh host \"psql -c 'drop table t'\"", [("psql", "drop table t")]),
    ("bash -c \"psql -c 'delete from t'\"", [("psql", "delete from t")]),
    ("echo $(psql -c 'select 1')", [("psql", "select 1")]),
    ("psql -c 'select 1' | head", [("psql", "select 1")]),
    ("psql -c 'select 1' > out.txt 2>&1", [("psql", "select 1")]),
    ("psql postgres://u:p#w@h/d -c 'delete from t'", [("psql", "delete from t")]),
    ("psql -f /definitely/missing.sql", [("psql-file", "<unreadable: file /definitely/missing.sql>")]),
    ("psql -c \"$SQL\"", [("psql", "$SQL")]),
    ("psql -c \"$(cat q.sql)\"", [("psql", "<unreadable: shell substitution>")]),
    ("psql <<'EOF'\nselect 1;\ndelete from t;\nEOF", [("psql-heredoc", "select 1;\ndelete from t;")]),
    ("psql -d db <<EOF\nselect 2;\nEOF", [("psql-heredoc", "select 2;")]),
    ("psql <<-EOF\n\tselect 3;\n\tEOF", [("psql-heredoc", "\tselect 3;")]),
    ("psql -f - <<'SQL'\ntruncate t;\nSQL", [("psql-heredoc", "truncate t;")]),
    ("psql <<< 'select 4'", [("psql-herestring", "select 4")]),
    ("echo 'delete from t' | psql", [("psql-stdin", "<unreadable: SQL piped on stdin>")]),
    ("cat q.sql | psql -d db", [("psql-stdin", "<unreadable: SQL piped on stdin>")]),
    ("bq query 'select 1'", [("bq", "select 1")]),
    ("bq query --nouse_legacy_sql 'delete from ds.t where true'", [("bq", "delete from ds.t where true")]),
    ("bq --project_id=p query --use_legacy_sql=false \"select 2\"", [("bq", "select 2")]),
    ("aws redshift-data execute-statement --cluster-identifier c --database d --sql 'select 1'", [("redshift-data", "select 1")]),
    ("aws redshift-data execute-statement --sql='delete from t'", [("redshift-data", "delete from t")]),
    ("aws --profile p redshift-data execute-statement --sql 'drop table t' --region r", [("redshift-data", "drop table t")]),
    ("aws redshift-data batch-execute-statement --sqls 'select 1' 'drop table t' --region r",
     [("redshift-data", "select 1"), ("redshift-data", "drop table t")]),
    ("aws redshift-data execute-statement --cli-input-json file://x.json", [("redshift-data", "<unreadable: no --sql>")]),
    ("aws athena start-query-execution --query-string 'select 1' --work-group w", [("athena", "select 1")]),
    ("aws athena start-query-execution --query-string='drop table t'", [("athena", "drop table t")]),
    ("aws athena start-query-execution --cli-input-json file://q.json", [("athena", "<unreadable: no --query-string>")]),
    ("psql -c 'select 1", [("shell", "<unreadable: shell parse error>")]),
    # M4.1: dea query keeps its behaviour; redirections are not arguments; other dea subcommands carry no SQL
    ('dea query warehouse "select 1" 2>&1 | head', [("dea:warehouse", "select 1")]),
    ('dea query warehouse "delete from t" 2>&1', [("dea:warehouse", "delete from t")]),
    ('dea query w "select 1" 2>/dev/null >out.txt', [("dea:w", "select 1")]),
    ('dea query --format json w "select 1" >&2', [("dea:w", "select 1")]),
    ('dea query w "select 1" &> out.txt', [("dea:w", "select 1")]),
    ("dea query --help 2>&1 | head -50", []),
    ("dea query --help", []),
    ("dea query w", [("dea", "<unreadable: dea query needs <source> <sql>>")]),
]

NO_DB = [
    "ls -la", "git status", "echo hello", "cat README.md", "grep psql README.md", "cat x | grep psql", "which psql",
    "aws s3 ls s3://bucket", "aws redshift-data describe-statement --id x", "aws athena get-query-execution --query-execution-id q",
    "python3 script.py", "bq ls", "psql --version",
    "dea --help 2>&1 | head", "dea snapshot 2>&1", "dea diff >&2", "dea plan check p1 2>&1 | tail -20", "dea scan > /tmp/x 2>&1",
    "dea find 'delete from t' 2>&1 | head", "dea plans &> out.txt",
]


@pytest.mark.parametrize("cmd,expected", CASES)
def test_extract(cmd, expected):
    assert extract_sql(cmd) == expected


@pytest.mark.parametrize("cmd", NO_DB)
def test_no_db_client(cmd):
    assert extract_sql(cmd) == []


def test_case_count():
    assert len(CASES) + len(NO_DB) >= 30


def test_psql_file_is_read(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("select 1;\ndelete from t;\n")
    got = extract_sql(f"psql -f {f}")
    assert got == [("psql-file", "select 1;\ndelete from t;\n")]
    assert [s.kind for s in classify(got[0][1])] == ["read", "write"]


def test_psql_file_relative_to_cwd(tmp_path):
    (tmp_path / "q.sql").write_text("select 1")
    assert extract_sql("psql --file=q.sql", cwd=str(tmp_path)) == [("psql-file", "select 1")]


def test_psql_stdin_redirect(tmp_path):
    (tmp_path / "q.sql").write_text("drop table t")
    assert extract_sql("psql < q.sql", cwd=str(tmp_path)) == [("psql-stdin", "drop table t")]


@pytest.mark.parametrize("cmd", [c for c, e in CASES if any("<unreadable" in s for _, s in e)])
def test_unreadable_classifies_as_write(cmd):
    for _, sql in extract_sql(cmd):
        if sql.startswith("<unreadable"):
            assert all(s.kind == "write" for s in classify(sql))
