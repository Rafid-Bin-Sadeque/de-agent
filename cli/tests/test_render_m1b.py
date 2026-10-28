import os
from pathlib import Path

import pytest
from de_agent import render as R
from test_airflow_collector import run as af_run
from test_dbt_collector import collect as dbt_collect

HERE = Path(__file__).parent
FIX = HERE / "fixtures"


def _cases():
    cases = {
        "af_healthy": lambda: af_run("airflow_2_10/healthy.json"),
        "af_failing": lambda: af_run("airflow_2_10/failing.json"),
        "af3_basic": lambda: af_run("airflow_3_0/basic.json"),
        "dbt_healthy": lambda: dbt_collect(FIX / "dbt" / "healthy"),
    }
    if (FIX / "dbt" / "failing").is_dir():
        cases["dbt_failing"] = lambda: dbt_collect(FIX / "dbt" / "failing")
    return cases


@pytest.mark.parametrize("name", sorted(_cases()))
def test_golden_and_deterministic(name):
    res = _cases()[name]()
    res["duration_s"] = 0.0
    got = R.render(res)
    golden = HERE / "golden" / f"{name}.md"
    if os.environ.get("DEA_UPDATE_GOLDEN"):
        golden.write_text(got)
    assert got == golden.read_text()
    assert R.render(_cases()[name]() | {"duration_s": 0.0}) == got
