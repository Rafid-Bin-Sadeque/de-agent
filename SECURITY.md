# Security policy

Report vulnerabilities privately (GitHub private security advisory on this repository, or email the maintainers). Do not open a public issue.

Scope: the guard hooks (`scripts/sql_guard.py`, `scripts/secret_guard.py`, `scripts/approval_guard.py`), the approval signature (`cli/src/de_agent/approval.py`) and the SQL classifier
(`cli/src/de_agent/sqlclass.py`, `extract.py`). Bypasses of the write-deny or secret-deny rules are in scope.
The guards are defence in depth. Read-only database roles remain the primary control.
