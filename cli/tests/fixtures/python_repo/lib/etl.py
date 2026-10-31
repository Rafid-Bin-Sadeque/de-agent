"""Helpers shared by the jobs (fictional)."""
import psycopg

STEPS = ["etl.sp_step_one", "etl.sp_step_two"]


def run_step(proc, conn=None):
    """Call a stored procedure."""
    conn = conn or psycopg.connect("dbname=warehouse")
    conn.cursor().execute(f"CALL {proc}()")


def nightly():
    for step in STEPS:
        run_step(step)


def vacuum_all():
    run_step("etl.sp_vacuum")
