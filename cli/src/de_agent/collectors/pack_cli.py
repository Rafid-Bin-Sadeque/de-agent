"""`type: cli` source: runs a CLI pack's argv lists with `shell=False` and parses the JSON they print.

Enforcement: the final argv (placeholders substituted) must pass the read-verb classifier (`packs/argv.py`) BEFORE any process is
started; a non-read argv is a pack error and `subprocess` is never called. `exec: ssh:<host>` runs `ssh <host> -- <quoted argv>`,
each argument `shlex.quote`d. Output is capped, a timeout applies per section.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime
from typing import Any

from de_agent import sqlexec  # module, not names: sqlexec -> collectors -> this module is a cycle
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.collectors.pack_base import (
    MAX_JSON_BYTES,
    PackCollector,
    apply_select,
    validate_for_config,
)
from de_agent.packs import argv as argv_rules
from de_agent.packs import template
from de_agent.packs.errors import PackError
from de_agent.packs.model import Pack, Section


def build_argv(sec: Section, params: dict[str, Any], item: str | None, now: datetime) -> list[str]:
    return [template.render(a, params, item, now) for a in sec.run or []]


def ssh_argv(host: str, argv: list[str]) -> list[str]:
    return [*sqlexec.SSH_BASE, host, "--", *[shlex.quote(a) for a in argv]]


def execute(argv: list[str], exec_target: str, timeout_s: float) -> str:
    cmd = ssh_argv(exec_target[4:], argv) if exec_target.startswith("ssh:") else argv
    try:
        p = subprocess.run(cmd, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout_s, env=dict(os.environ))
    except FileNotFoundError:
        raise CollectorError(f"{argv[0]} not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise CollectorError(f"{argv[0]} timed out after {int(timeout_s)}s") from None
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-3:])
        raise CollectorError(sqlexec.redact(tail or f"{argv[0]} exited {p.returncode}"))
    return p.stdout


def parse_json(out: str) -> Any:
    if len(out) > MAX_JSON_BYTES:
        raise CollectorError("command output larger than 5 MB")
    text = out.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except ValueError:
        raise CollectorError("command output was not JSON (add the CLI's JSON output flag to `run:`)") from None


class CliPackCollector(PackCollector):
    type = "cli"

    def validate_source(self, source: Any) -> None:
        validate_for_config(source, self.type)
        if source.conn is not None:
            raise ValueError("cli sources take no `conn:`; the pack's argv runs locally or over `exec: ssh:<host>`")

    def run_section(self, state: Any, source: Any, ctx: RunContext, pack: Pack, sec: Section, item: str | None,
                    params: dict[str, Any], now: datetime) -> list[Any]:
        left = ctx.remaining()
        if left <= 0:
            raise CollectorError("source timeout budget exhausted")
        argv = build_argv(sec, params, item, now)
        why = argv_rules.refusal(argv)  # BEFORE any process is started
        if why:
            raise PackError(f"refused, not a read-only command: {why}")
        out = ctx.pack_fn(sec, item, argv) if ctx.pack_fn is not None else execute(argv, source.exec, min(left, 60))
        doc = parse_json(out) if isinstance(out, str) else out
        return apply_select(doc, sec)

    def ping_kind(self, source: Any, ctx: RunContext, pack: Pack) -> str:
        exe = pack.requires.cli or (pack.sections[0].run or [""])[0]
        if source.exec.startswith("ssh:"):
            return f"pack {pack.pack} ok (`{exe}` is checked on the remote host by dea snapshot)"
        if shutil.which(exe) is None:
            raise CollectorError(f"`{exe}` is not on PATH (install it, or use exec: ssh:<host>)")
        return f"pack {pack.pack} ok, `{exe}` found"
