"""Locate the hub (M1a section 2). First match wins: $DEA_HUB, nearest de-agent.yaml upward, user config."""
from __future__ import annotations

import os
import re
from pathlib import Path

CONFIG_NAME = "de-agent.yaml"
USER_CONFIG = Path("~/.config/de-agent/config.yaml")
HINT = "no hub found: run `dea init` or set DEA_HUB"


def find_hub(start: Path | None = None) -> Path | None:
    env = os.environ.get("DEA_HUB")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    cur = (start or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        if (d / CONFIG_NAME).is_file():
            return d
    uc = USER_CONFIG.expanduser()
    try:
        text = uc.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^hub:\s*[\"']?([^\"'\n#]+?)[\"']?\s*(?:#.*)?$", text, re.M)
    if m:
        p = Path(m.group(1).strip()).expanduser()
        if p.is_absolute() and p.is_dir():
            return p
    return None
