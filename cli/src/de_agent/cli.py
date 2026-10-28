"""dea command-line entry point."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from de_agent.sqlclass import classify

app = typer.Typer(help="de-agent: ground-truth context and guardrails for data engineering.")


def _require_hub() -> Path:
    from de_agent.hubpath import HINT, find_hub

    hub = find_hub()
    if hub is None:
        typer.echo(HINT, err=True)
        raise typer.Exit(2)
    return hub


def _load(hub: Path):  # noqa: ANN202
    from de_agent.config import ConfigError, load_config

    try:
        return load_config(hub / "de-agent.yaml")
    except ConfigError as ex:
        typer.echo(f"config error: {ex}", err=True)
        raise typer.Exit(2) from None


@app.command("init")
def init(
    dir: Path = typer.Argument(Path("."), help="Directory to scaffold the hub in"),
    name: str = typer.Option("My data platform", "--name", help="Platform name"),
    with_example: bool = typer.Option(False, "--with-example", help="Add a small generic example project"),
) -> None:
    """Scaffold a hub (de-agent.yaml, map, status, conventions, templates). Never overwrites."""
    from de_agent.initcmd import init_hub

    created, skipped = init_hub(dir, name, with_example)
    for rel in created:
        typer.echo(f"created  {rel}")
    if skipped:
        typer.echo("skipped (already exist): " + ", ".join(skipped))
    typer.echo(f"hub ready at {dir.resolve()}")


@app.command("snapshot")
def snapshot(
    source: list[str] | None = typer.Option(None, "--source", help="Only this source (repeatable)"),
    no_render: bool = typer.Option(False, "--no-render", help="Write JSON only, skip platform/*.md"),
    deep: bool = typer.Option(False, "--deep", help="Recompute heavy sections instead of reusing recent ones (redshift)"),
) -> None:
    """Collect every source concurrently and write .dea/snapshots + platform/*.md."""
    from de_agent.config import ConfigError
    from de_agent.render import signals_for
    from de_agent.runner import result_status
    from de_agent.snapshot import take_snapshot

    hub = _require_hub()
    cfg = _load(hub)
    if not cfg.sources:
        typer.echo("no sources configured in de-agent.yaml", err=True)
        raise typer.Exit(2)
    try:
        out = take_snapshot(hub, cfg, only=source or None, do_render=not no_render, deep=deep)
    except ConfigError as ex:
        typer.echo(str(ex), err=True)
        raise typer.Exit(2) from None
    for r in out.results:
        typer.echo(f"{r['source']}: {result_status(r)} ({len(signals_for(r))} signals, {r['duration_s']}s)")
        for e in r["errors"]:
            typer.echo(f"  error: {e}", err=True)
    typer.echo(f"snapshot {out.snapshot_id}")
    raise typer.Exit(1 if out.failed else 0)


@app.command("diff")
def diff(
    source: str | None = typer.Option(None, "--source"),
    since: str | None = typer.Option(None, "--since", help="Snapshot id to compare against"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """What changed between the latest snapshot and the previous one."""
    from de_agent.diff import DiffError, build_diff, render_markdown

    hub = _require_hub()
    try:
        report = build_diff(hub, source, since)
    except DiffError as ex:
        typer.echo(str(ex), err=True)
        raise typer.Exit(2) from None
    typer.echo(json.dumps(report, indent=2, default=str) if as_json else render_markdown(report).rstrip("\n"))


@app.command("lint")
def lint(as_json: bool = typer.Option(False, "--json")) -> None:
    """Check the hub: budgets, links, frontmatter, staleness, secrets. Exit 1 on any error."""
    from de_agent.lint import run_lint

    hub = _require_hub()
    findings = run_lint(hub)
    errors = [f for f in findings if f.level == "error"]
    if as_json:
        typer.echo(json.dumps([f.as_dict() for f in findings], indent=2))
    else:
        for f in findings:
            typer.echo(f"{f.level.upper():7} {f.rule:19} {f.path}: {f.message}")
        typer.echo(f"{len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    raise typer.Exit(1 if errors else 0)


@app.command("find")
def find(
    query: str = typer.Argument(..., help="Identifier or words, e.g. analytics.fct_orders"),
    n: int = typer.Option(10, "-n", help="Max results"),
    as_json: bool = typer.Option(False, "--json"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force a full reindex"),
) -> None:
    """Search hub pages and platform snapshots (exact identifiers first)."""
    from de_agent.find import find as do_find

    hub = _require_hub()
    hits = do_find(hub, query, n, rebuild)
    if as_json:
        typer.echo(json.dumps([h.as_dict() for h in hits], indent=2))
    else:
        for h in hits:
            typer.echo(f"{h.path}:{h.line}  {h.snippet}")
        if not hits:
            typer.echo(f"no hits for {query}")
    raise typer.Exit(0 if hits else 1)


@app.command("scan")
def scan(
    name: list[str] | None = typer.Option(None, "--name", help="Only this scan (repeatable)"),
    full: bool = typer.Option(False, "--full", help="Ignore the per-file cache and re-parse everything"),
) -> None:
    """Statically scan configured repos into catalog/, refresh profile headers and the find index."""
    from de_agent.scanners.run import run_scan

    hub = _require_hub()
    cfg = _load(hub)
    if not cfg.scans:
        typer.echo("no scans configured in de-agent.yaml (add a `scans:` list)", err=True)
        raise typer.Exit(2)
    out = run_scan(hub, cfg, only=name or None, full=full)
    for s in out.scans:
        if s["status"] == "ok":
            st = s["stats"]
            extra = ", ".join(f"{k}={v}" for k, v in st.items() if k.startswith(("dags", "models", "objects", "unresolved")))
            typer.echo(f"{s['name']}: {s['type']} ok, {s['files']} files ({s['parsed']} parsed, {s['cached']} cached), "
                       f"{s['duration_s']}s, rev {s['revision']}; {extra}")
        elif s["status"] == "skipped":
            typer.echo(f"warning: {s['name']}: {s['type']} {s['error']}", err=True)
        else:
            typer.echo(f"{s['name']}: {s['type']} ERROR {s['error']}", err=True)
    for proj, status in sorted(out.profiles.items()):
        typer.echo(f"profile {proj}: {status}")
    st = out.edges.get("stats", {})
    typer.echo(f"catalog: {out.pages} files; tables: {st.get('tables', 0)}, cross-project edges: {st.get('cross_project_edges', 0)}")
    shown = {s.get("error") for s in out.scans}
    for e in out.errors:
        if e not in shown:
            typer.echo(f"error: {e}", err=True)
    raise typer.Exit(1 if out.failed else 0)


def _load_edges(hub: Path) -> dict:
    p = hub / "catalog" / "edges.json"
    if not p.is_file():
        typer.echo("catalog/edges.json not found: run `dea scan` first", err=True)
        raise typer.Exit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def _load_symbols(hub: Path) -> dict:
    p = hub / "catalog" / "symbols.json"
    if not p.is_file():
        return {}
    flat: dict = {}
    for scan_name, syms in json.loads(p.read_text(encoding="utf-8")).get("scans", {}).items():
        for q, s in syms.items():
            flat[q] = {**s, "scan": scan_name}
    return flat


def _unknown(kind: str, name: str, pool: list[str]) -> None:
    import difflib

    names = {p.lower(): p for p in pool}
    close = difflib.get_close_matches(name.lower(), list(names), n=5, cutoff=0.4)
    typer.echo(f"unknown {kind}: {name}", err=True)
    if close:
        typer.echo("closest: " + ", ".join(names[c] for c in close), err=True)
    raise typer.Exit(1)


def _resolve_table(data: dict, name: str) -> str | None:
    """`schema.table` as given, or an unqualified name that matches exactly one table. Ambiguity exits 1 with the candidates."""
    key = name.lower()
    if key in data["tables"]:
        return key
    if "." in key:
        return None
    cands = sorted(t for t in data["tables"] if t.rsplit(".", 1)[-1] == key)
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        typer.echo(f"ambiguous table {name}: matches {', '.join(cands)}; qualify it as schema.table", err=True)
        raise typer.Exit(1)
    return None


@app.command("edges")
def edges(table: str = typer.Argument(..., help="schema.table, e.g. analytics.fct_orders")) -> None:
    """Who writes and reads a table (from catalog/edges.json)."""
    hub = _require_hub()
    data = _load_edges(hub)
    key = _resolve_table(data, table)
    if key is None:
        _unknown("table", table, sorted(data["tables"]))
    t = data["tables"][key]
    typer.echo(key)
    for label, items in (("writers", t["writers"]), ("readers", t["readers"])):
        typer.echo(f"{label}:" + ("" if items else " none"))
        for e in items:
            extra = f", proc {e['proc']}" if e.get("proc") else ""
            extra += ", unrolled" if e.get("unrolled") else ""
            extra += ", heuristic" if e.get("heuristic") else ""
            extra += ", pattern" if e.get("pattern") else ""
            typer.echo(f"  {e['id']} [{e['project']}] via {e['via']}{extra}  {e.get('file', '')}")


@app.command("impact")
def impact(
    target: str = typer.Argument(..., help="util symbol (module.func) or schema.table"),
    depth: int = typer.Option(5, "--depth", min=1, max=10, help="Max downstream hops for a table (1-10)"),
) -> None:
    """Symbol: the DAGs that use it. Table: downstream readers, transitively, with the path."""
    from de_agent.scanners.edges import downstream

    hub = _require_hub()
    key = target.lower()
    data = _load_edges(hub)
    syms = _load_symbols(hub)
    if key not in data["tables"] and not any(q == target or q.endswith("." + target) for q in syms):
        key = _resolve_table(data, target) or key
    if key in data["tables"]:
        rows = downstream(data, key, depth)
        typer.echo(f"impact of table {key} (downstream readers, depth <= {depth}): {len(rows)}")
        for r in sorted(rows, key=lambda r: (r["depth"], r["reader"])):
            typer.echo(f"  depth {r['depth']}  {r['reader']} [{r['project']}]  {' -> '.join(r['path'])}")
        return
    match = [q for q in syms if q == target or q.endswith("." + target)]
    if match:
        for q in sorted(match):
            s = syms[q]
            typer.echo(f"impact of symbol {q} ({s['file']}:{s['line']}): used by {len({d['dag_id'] for d in s['dags']})} DAGs")
            for d in s["dags"]:
                typer.echo(f"  {d['dag_id']}  {d['file']}")
        return
    _unknown("table or symbol", target, [*data["tables"], *syms])


def _fmt_cell(v: object) -> str:
    return "" if v is None else (json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v))


def _print_table(rows: list[dict]) -> None:
    if not rows:
        typer.echo("(0 rows)")
        return
    cols = list(rows[0])
    cells = [[_fmt_cell(r.get(c))[:60] for c in cols] for r in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(cols)]
    typer.echo("  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip())
    typer.echo("  ".join("-" * w for w in widths))
    for r in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())


def _source_dialect(source: str) -> str:
    """SQL dialect for classifying `dea query` input: redshift for redshift sources, else postgres."""
    from de_agent.hubpath import find_hub

    try:
        hub = find_hub()
        if hub is not None:
            from de_agent.config import load_config

            src = load_config(hub / "de-agent.yaml").source(source)
            return "redshift" if src.type == "redshift" else "postgres"
    except Exception:
        pass
    return "postgres"


@app.command("query")
def query(
    source: str = typer.Argument(..., help="Source name from de_agent.yaml"),
    sql: str = typer.Argument(..., help="One read-only statement"),
    fmt: str = typer.Option("table", "--format", help="table | json | csv"),
    limit: int = typer.Option(200, "--limit", min=1, help="Max rows returned"),
) -> None:
    """Run one read-only statement on a configured source (read-only transaction, timeouts, row cap)."""
    import csv

    from de_agent.collectors.base import CollectorError
    from de_agent.config import ConfigError
    from de_agent.sqlexec import query_limited

    if fmt not in ("table", "json", "csv"):
        typer.echo("--format must be table, json or csv", err=True)
        raise typer.Exit(2)
    dialect = _source_dialect(source)
    stmts = classify(sql, dialect)
    writes = [st for st in stmts if st.kind != "read"]
    if writes or not stmts:
        why = f"{writes[0].label} is a write" if writes else "empty statement"
        typer.echo(f"refused: {why}; dea query is read-only (writes go through a plan: dea plan check, dea approve, dea apply)",
                   err=True)
        raise typer.Exit(2)
    if len(stmts) != 1:
        typer.echo(f"refused: dea query runs exactly one statement (got {len(stmts)})", err=True)
        raise typer.Exit(2)
    hub = _require_hub()
    cfg = _load(hub)
    try:
        src = cfg.source(source)
    except ConfigError:
        typer.echo(f"unknown source {source!r}; configured: {', '.join(s.name for s in cfg.sources if s.conn or s.data_api) or 'none'}",
                   err=True)
        raise typer.Exit(2) from None
    if src.conn is None and src.data_api is None:
        typer.echo(f"source {source!r} is not a SQL source (type {src.type})", err=True)
        raise typer.Exit(2)
    if src.writable:
        typer.echo(f"source {source!r} is a write-capable source for plans; query a read-only source instead", err=True)
        raise typer.Exit(2)
    try:
        if src.type == "redshift":
            from de_agent.sqlexec import query_limited_redshift

            rows, total = query_limited_redshift(src, sql, float(src.timeout_seconds), limit)
        else:
            rows, total = query_limited(src.conn, sql, src.exec, float(src.timeout_seconds), limit)
    except CollectorError as ex:
        typer.echo(f"query failed: {ex}", err=True)
        raise typer.Exit(1) from None
    except Exception as ex:  # never a traceback for the caller (an agent reads this output)
        typer.echo(f"query failed: {type(ex).__name__}: {ex}", err=True)
        raise typer.Exit(1) from None
    more = total - len(rows)
    if fmt == "json":
        typer.echo(json.dumps({"rows": rows, "total": total, "more_rows": max(more, 0)}, default=str, indent=2))
        return
    if fmt == "csv":
        import io

        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                w.writerow({k: _fmt_cell(v) for k, v in r.items()})
        typer.echo(buf.getvalue().rstrip("\n"))
    else:
        _print_table(rows)
    if more > 0:
        typer.echo(f"({more} more rows not shown; raise --limit)", err=fmt == "csv")


plan_app = typer.Typer(help="Plan files: .dea/plans/<id>.md")
app.add_typer(plan_app, name="plan")


def _plan_and_cfg(plan_id: str):  # noqa: ANN202
    from de_agent.plans import PlanError, load_plan

    hub = _require_hub()
    cfg = _load(hub)
    try:
        return hub, cfg, load_plan(hub, plan_id)
    except PlanError as ex:
        typer.echo(str(ex), err=True)
        raise typer.Exit(2) from None


@plan_app.command("check")
def plan_check(plan_id: str = typer.Argument(..., help="Plan id (file name without .md)"),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Classify every statement in a plan and list errors. Exit 1 on any error."""
    from datetime import datetime, timezone

    from de_agent.plans import check_plan, render_report, write_checked

    hub, cfg, plan = _plan_and_cfg(plan_id)
    rep = check_plan(plan, cfg, filename_id=plan_id.removesuffix(".md"), hub=hub)
    if as_json:
        typer.echo(json.dumps({**rep.as_dict(), "hashes": rep.hashes}, indent=2))
    else:
        typer.echo(render_report(plan, rep))
    if rep.ok:
        write_checked(hub, plan, rep, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    raise typer.Exit(0 if rep.ok else 1)


@app.command("approve")
def approve(plan_id: str = typer.Argument(..., help="Plan id")) -> None:
    """HUMAN ONLY: review the plan check, type the plan id, sign the statement hashes (needs a TTY)."""
    from datetime import datetime, timezone

    from de_agent import approval
    from de_agent.plans import check_plan, plan_hashes, plans_dir, render_report

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo("dea approve is human-only and needs an interactive terminal: run this in your own terminal.", err=True)
        raise typer.Exit(2)
    hub, cfg, plan = _plan_and_cfg(plan_id)
    rep = check_plan(plan, cfg, filename_id=plan_id.removesuffix(".md"), hub=hub)
    typer.echo(render_report(plan, rep))
    if not rep.ok:
        typer.echo("\nrefusing to approve a plan with errors", err=True)
        raise typer.Exit(1)
    typer.echo("\n" + plan.text.split("\n## Rollback")[0][:6000])
    typed = input(f"\nType the plan id ({plan.id}) to approve it, anything else to cancel: ").strip()
    if typed != plan.id:
        typer.echo("cancelled; nothing written", err=True)
        raise typer.Exit(1)
    try:
        who = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, cwd=hub,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        who = ""
    who = who or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    key = approval.load_key(create=True)
    data = approval.make_approval(plan.id, plan_hashes(plan), who,
                                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), key)
    out = plans_dir(hub) / f"{plan.id}.approved"
    approval.write_approval(str(out), data)
    typer.echo(f"approved {plan.id} ({len(data['hashes'])} statement hashes) by {who}: {out}")


@app.command("apply")
def apply(
    plan_id: str = typer.Argument(..., help="Plan id"),
    rollback: bool = typer.Option(False, "--rollback", help="Run the Rollback section instead"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would run"),
) -> None:
    """Run an approved plan step by step (executor agent only; verifies the HMAC and every statement hash first)."""
    from de_agent import approval
    from de_agent.applyplan import ApplyRefused, apply_plan, authorize, dry_run_lines

    hub, cfg, plan = _plan_and_cfg(plan_id)
    key = approval.load_key()
    try:
        if dry_run:
            authorize(hub, cfg, plan, rollback, key)
            typer.echo("\n".join(dry_run_lines(plan, rollback)))
            typer.echo("dry run: nothing executed")
            return
        res = apply_plan(hub, cfg, plan, rollback=rollback, key=key)
    except ApplyRefused as ex:
        typer.echo(f"refused: {ex}", err=True)
        raise typer.Exit(2) from None
    for st in res.steps:
        line = f"step {st['step']} {st['status']}  rowcount={st['rowcount']}  {st['duration_s']}s"
        typer.echo(line + (f"  error: {st['error']}" if st["error"] else ""))
        for act in st.get("actions", []):
            if act["kind"] == "diff":
                typer.echo(f"  {act['mode']} diff {act['repo']}/{act['file']}: {act.get('diff_stat') or '(no stat)'}".replace("\n", " | "))
            else:
                typer.echo(f"  shell exit={act['exit_code']}: {act['command'][:100]}")
        for v in st["verify"]:
            typer.echo(f"  verify {'ok' if v['ok'] else 'FAILED'}: value={v['value']!r} expect={v['expect']}")
    typer.echo(f"{res.mode} {res.status}: {res.path}")
    raise typer.Exit(0 if res.status == "ok" else 1)


@app.command("plans")
def plans_cmd() -> None:
    """List plans with their state: draft / checked / approved / applied / failed / rolled back."""
    from de_agent import approval
    from de_agent.plans import load_plan, plan_state, plans_dir

    hub = _require_hub()
    key = approval.load_key()
    paths = sorted(plans_dir(hub).glob("*.md")) if plans_dir(hub).is_dir() else []
    if not paths:
        typer.echo("no plans (.dea/plans/ is empty)")
        return
    for p in paths:
        try:
            pl = load_plan(hub, p.stem)
            meta = f"{pl.risk or '?':6} {pl.title}"
        except Exception:
            meta = "(unreadable)"
        typer.echo(f"{p.stem}  {plan_state(hub, p.stem, key):12} {meta}")


def _hook_python() -> str:
    """The python3 the hooks will run: DEA_HOOK_PYTHON, else the first `python3` on PATH once this venv's bin dir is
    removed (a pipx/uv tool venv is not what Claude Code's hook command resolves)."""
    env = os.environ.get("DEA_HOOK_PYTHON")
    if env:
        return env
    skip = {os.path.realpath(os.path.dirname(sys.executable))} if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else set()
    if os.environ.get("VIRTUAL_ENV"):
        skip.add(os.path.realpath(os.path.join(os.environ["VIRTUAL_ENV"], "bin")))
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.realpath(d) not in skip)
    return shutil.which("python3", path=path) or "python3"


def _plugin_root() -> Path | None:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    cands = [Path(env)] if env else []
    cands.append(Path(__file__).resolve().parents[3])
    return next((c for c in cands if (c / "scripts" / "_vendor").is_dir()), None)


@app.command("doctor")
def doctor() -> None:
    """Check the environment: python, hub, config, sources, psql, FTS5, hook python + vendored sqlglot."""
    from de_agent.collectors import get_collector
    from de_agent.collectors.base import RunContext
    from de_agent.config import ConfigError, load_config
    from de_agent.find import fts5_available
    from de_agent.hubpath import find_hub

    rows: list[tuple[str, str, str]] = []  # (mark, name, detail)

    def add(ok: bool | None, name: str, detail: str) -> None:
        rows.append(("✓" if ok else ("✗" if ok is False else "-"), name, detail))

    v = sys.version_info
    add(v >= (3, 10), "python", f"{v.major}.{v.minor}.{v.micro} (CLI needs >= 3.10)")
    hub = find_hub()
    add(hub is not None, "hub", str(hub) if hub else "not found (dea init, or set DEA_HUB)")
    cfg = None
    if hub:
        try:
            cfg = load_config(hub / "de-agent.yaml")
            add(True, "config", f"valid, {len(cfg.sources)} source(s)")
        except ConfigError as ex:
            add(False, "config", str(ex))
    for s in cfg.sources if cfg else []:
        try:
            detail = get_collector(s.type).ping(s, RunContext(timeout_seconds=15))
            add(True, f"source {s.name}", f"{s.type} reachable via {s.exec} ({detail})")
        except Exception as ex:
            add(False, f"source {s.name}", str(ex).splitlines()[-1][:120] if str(ex) else type(ex).__name__)
    psql = shutil.which("psql")
    add(psql is not None, "psql", psql or "not on PATH (needed for ssh sources and the no-psycopg fallback)")
    add(fts5_available(), "fts5", "SQLite FTS5 available" if fts5_available() else "missing: dea find falls back to a slow scan")
    py = _hook_python()
    try:
        out = subprocess.run([py, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        major, minor = (int(x) for x in out.split("."))
        add((major, minor) >= (3, 9), "hook python", f"{py} is {out} (the python3 the hooks run; needs >= 3.9)")
    except Exception as ex:
        add(False, "hook python", f"cannot run {py}: {ex}")
        py = ""
    root = _plugin_root()
    if root and py:
        vend = root / "scripts" / "_vendor"
        r = subprocess.run([py, "-c", "import sys;sys.path.insert(0, sys.argv[1]);import sqlglot;print(sqlglot.__version__)", str(vend)],
                           capture_output=True, text=True, timeout=30)
        add(r.returncode == 0, "vendored sqlglot", f"v{r.stdout.strip()} under {py}" if r.returncode == 0 else r.stderr.strip()[-100:])
    else:
        add(None, "vendored sqlglot", "not checked: set CLAUDE_PLUGIN_ROOT=<your clone of the plugin> to check it")
    width = max(len(n) for _, n, _ in rows)
    for mark, name, detail in rows:
        typer.echo(f"{mark} {name.ljust(width)}  {detail}")
    raise typer.Exit(1 if any(m == "✗" for m, _, _ in rows) else 0)


config_app = typer.Typer(help="de-agent.yaml tooling.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("schema")
def config_schema(
    markdown: bool = typer.Option(False, "--markdown", help="Markdown reference (docs/configuration.md) instead of JSON Schema"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write to this file instead of stdout"),
) -> None:
    """Print the de-agent.yaml JSON Schema (default) or its markdown reference, generated from the config models."""
    from de_agent.configdoc import json_text
    from de_agent.configdoc import markdown as md_text

    text = md_text() if markdown else json_text()
    if out:
        out.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text.rstrip("\n"))


@app.command("guard-test")
def guard_test(
    corpus: Path | None = typer.Option(None, help="YAML corpus of SQL classification cases (default: the one shipped in the package)"),
) -> None:
    """Run the SQL classifier corpus (the packaged one unless --corpus is given)."""
    import yaml

    if corpus is None:
        corpus = Path(__file__).resolve().parent / "data" / "sql_cases.yaml"
    cases = yaml.safe_load(corpus.read_text())
    failed = 0
    for c in cases:
        got = classify(c["sql"], c.get("dialect", "postgres"))
        kinds = [s.kind for s in got]
        want = c["kind"] if isinstance(c["kind"], list) else [c["kind"]]
        if kinds != want:
            failed += 1
            typer.echo(f"FAIL {json.dumps(c['sql'])[:80]}: want {want} got {kinds}")
    typer.echo(f"{len(cases) - failed}/{len(cases)} passed")
    raise typer.Exit(1 if failed else 0)


@app.callback()
def main() -> None:
    """de-agent CLI."""
