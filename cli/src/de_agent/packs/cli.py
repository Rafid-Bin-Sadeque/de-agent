"""`dea pack ...` commands."""
from __future__ import annotations

from pathlib import Path

import typer

from de_agent.packs.check import check_text
from de_agent.packs.errors import PackError
from de_agent.packs.loader import find_pack, hub_packs_dir, list_packs
from de_agent.packs.model import KINDS, NAME_RE

pack_app = typer.Typer(help="Packs: declarative sources (a YAML file that describes what to read from a SQL, CLI or HTTP system).",
                       no_args_is_help=True)


@pack_app.command("list")
def pack_list() -> None:
    """Every pack you can use: built-in and hub-local (a hub file with a built-in's name overrides it)."""
    refs = list_packs()
    rows = []
    for r in refs:
        try:
            p = r.parse()
            rows.append((r.name, p.kind, p.validated, r.source))
        except PackError:
            rows.append((r.name, "?", "?", r.source + " (invalid)"))
    w = [max(len(h), *(len(x[i]) for x in rows)) for i, h in enumerate(("name", "kind", "validated", "source"))]
    for row in [("name", "kind", "validated", "source"), *rows]:
        typer.echo("  ".join(c.ljust(w[i]) for i, c in enumerate(row)).rstrip())


@pack_app.command("show")
def pack_show(name: str = typer.Argument(..., help="Pack name")) -> None:
    """Print a pack's YAML (the hub's file when it overrides a built-in)."""
    try:
        ref = find_pack(name)
    except PackError as ex:
        typer.echo(str(ex), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"# {ref.source}: {ref.path}")
    typer.echo(ref.text.rstrip("\n"))


@pack_app.command("new")
def pack_new(
    name: str = typer.Argument(..., help="Pack name: [a-z0-9][a-z0-9_-]*"),
    kind: str = typer.Option(..., "--kind", help="sql | cli | http"),
) -> None:
    """Scaffold packs/<name>.yaml in the hub (or ./packs when there is no hub), with comments. Never overwrites."""
    from de_agent.packs.scaffold import scaffold

    if not NAME_RE.match(name):
        typer.echo("pack name must match [a-z0-9][a-z0-9_-]*", err=True)
        raise typer.Exit(2)
    if kind not in KINDS:
        typer.echo(f"--kind must be one of {', '.join(KINDS)}", err=True)
        raise typer.Exit(2)
    target = hub_packs_dir() / f"{name}.yaml"
    if target.exists():
        typer.echo(f"{target} already exists; not overwriting", err=True)
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(scaffold(name, kind), encoding="utf-8")
    typer.echo(f"created {target}")
    typer.echo(f"next: edit it, then `dea pack check {name}`")


def _check_one(label: str, text: str) -> bool:
    pack, errs = check_text(text)
    if errs:
        typer.echo(f"FAIL {label}")
        for e in errs:
            typer.echo(f"  - {e}")
        return False
    assert pack is not None
    typer.echo(f"ok   {label} ({pack.kind}, {len(pack.sections)} sections, validated: {pack.validated})")
    return True


@pack_app.command("check")
def pack_check(
    target: str = typer.Argument(None, help="Pack name, or path to a pack file"),
    all_packs: bool = typer.Option(False, "--all", help="Check every built-in and hub pack"),
) -> None:
    """Validate schema and expressions; classify every SQL as a read in the pack's dialect; run the argv read-verb check
    and the http method/GraphQL checks. Exit 1 when anything fails."""
    ok = True
    if all_packs:
        for r in list_packs():
            ok = _check_one(f"{r.name} [{r.source}]", r.text) and ok
        raise typer.Exit(0 if ok else 1)
    if not target:
        typer.echo("give a pack name or file, or use --all", err=True)
        raise typer.Exit(2)
    p = Path(target)
    if p.is_file():
        text = p.read_text(encoding="utf-8")
    else:
        try:
            text = find_pack(target).text
        except PackError as ex:
            typer.echo(str(ex), err=True)
            raise typer.Exit(2) from None
    raise typer.Exit(0 if _check_one(target, text) else 1)


@pack_app.command("test")
def pack_test(source: str = typer.Argument(..., help="Source name from de-agent.yaml (type sql, cli or http)")) -> None:
    """Run the source once, read-only, and print the rendered page plus the signals. Writes no snapshot."""
    from de_agent.cli import _load, _require_hub
    from de_agent.config import ConfigError
    from de_agent.render import render, signals_for
    from de_agent.runner import result_status, run_source

    hub = _require_hub()
    cfg = _load(hub)
    try:
        src = cfg.source(source)
    except ConfigError:
        typer.echo(f"unknown source {source!r}", err=True)
        raise typer.Exit(2) from None
    if src.type not in ("sql", "cli", "http"):
        typer.echo(f"source {source!r} is type {src.type}, not a pack source", err=True)
        raise typer.Exit(2)
    result = run_source(src)
    typer.echo(render(result).rstrip("\n"))
    typer.echo("")
    sigs = signals_for(result)
    typer.echo(f"signals ({len(sigs)}):")
    for s in sigs:
        typer.echo(f"  - {s}")
    for e in result["errors"]:
        typer.echo(f"error: {e}", err=True)
    status = result_status(result)
    typer.echo(f"status: {status}, {result['duration_s']}s")
    raise typer.Exit(1 if status == "failed" else 0)


def doctor_rows() -> list[tuple[bool, str, str]]:
    """(ok, name, detail) rows for `dea doctor`: hub-local packs must pass `pack check`."""
    d = hub_packs_dir()
    out = []
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        _, errs = check_text(p.read_text(encoding="utf-8"))
        out.append((not errs, f"pack {p.stem}", "passes pack check" if not errs else errs[0][:110]))
    return out
