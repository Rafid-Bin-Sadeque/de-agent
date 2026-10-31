"""Orchestration helpers that call the db helpers (fictional)."""
from . import db
from .db import run_proc

TABLES = ["orders", "customers"]


def refresh(name):
    """Refresh one table through its stored procedure (two hops from a DAG)."""
    db.run_proc(f"etl.sp_refresh_{name}")


def refresh_all():
    """Three hops from a DAG: refresh_all -> refresh -> run_proc."""
    for t in TABLES:
        refresh(t)


def bookkeeping():
    cur = None
    cur.execute("update etl.load_state set last_run = now() where job = 'x'")
    run_proc("etl.sp_audit")
