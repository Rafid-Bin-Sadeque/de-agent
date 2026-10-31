"""Every module imports on its own in a fresh interpreter (the unit suite imports `collectors` first and hides cycles)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
MODULES = sorted(
    ".".join(p.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__")
    for p in (SRC / "de_agent").rglob("*.py") if "_vendor" not in p.parts
)


@pytest.mark.parametrize("mod", MODULES)
def test_module_imports_cold(mod):
    p = subprocess.run([sys.executable, "-c", f"import {mod}"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-800:]
