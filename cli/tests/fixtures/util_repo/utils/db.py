"""Warehouse helpers shared by the DAGs (fictional Acme Shop)."""


def _cursor():
    return None


def run_proc(proc):
    """Run a stored procedure by name."""
    cur = _cursor()
    cur.execute(f"CALL {proc}()")


def load_orders():
    """Stage the raw order feed."""
    cur = _cursor()
    cur.execute("insert into staging.orders_stage select * from raw.orders_feed")


def copy_table(src, dst="staging.copy_target"):
    """Copy one table into another."""
    cur = _cursor()
    cur.execute(f"insert into {dst} select * from {src}")
