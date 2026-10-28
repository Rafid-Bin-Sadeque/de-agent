"""Postgres helpers shared by the DAGs (fictional Acme Shop)."""


def copy_df(df, table):
    """Copy a dataframe into a table."""
    return len(df)


def get_conn(conn_id="warehouse"):
    """Open a warehouse connection."""
    return conn_id


class Loader:
    """Incremental loader."""

    def run(self):
        return 1


def _private_helper():
    return 0
