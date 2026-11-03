# Security policy

Report vulnerabilities privately through GitHub: https://github.com/Rafid-Bin-Sadeque/de-agent/security/advisories/new. Do not open a public issue.

Scope: the guard hooks (`scripts/sql_guard.py`, `scripts/secret_guard.py`, `scripts/approval_guard.py`, `scripts/cli_guard.py`), the approval signature (`cli/src/de_agent/approval.py`) and the SQL classifier
(`cli/src/de_agent/sqlclass.py`, `extract.py`) and the CLI verb classifier (`cliclass.py`). Bypasses of the write-deny or secret-deny rules are in scope.
The guards are defence in depth. Read-only database roles remain the primary control.
