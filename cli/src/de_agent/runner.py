"""Run collectors in parallel with per-source timeouts, locally or over ssh (M1a sections 3-4, 7)."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable

from de_agent.collectors import get_collector
from de_agent.collectors.base import RunContext, make_result, utc_iso
from de_agent.config import SourceConfig
from de_agent.sqlexec import redact

MAX_WORKERS = 4


def result_status(result: dict[str, Any]) -> str:
    """ok | partial (some sections failed, some data) | failed (no data at all)."""
    sections = [k for k in (result.get("data") or {}) if k != "meta"]
    if not sections:
        return "failed"
    return "partial" if result.get("errors") else "ok"


def run_source(
    source: SourceConfig, query_fn: Callable[..., list[dict[str, Any]]] | None = None,
    prior: dict[str, Any] | None = None, deep: bool = False,
) -> dict[str, Any]:
    collector = get_collector(source.type)
    ctx = RunContext(timeout_seconds=source.timeout_seconds, query_fn=query_fn, prior=prior, deep=deep)
    collected_at = utc_iso()
    t0 = time.monotonic()
    try:
        out = collector.collect(source, ctx)
        data, errors = out.get("data", {}), list(out.get("errors", []))
    except Exception as ex:  # a collector bug must not take down the other sources
        data, errors = {}, [f"collector crashed: {type(ex).__name__}: {redact(str(ex))}"]
    return make_result(
        source.name, source.type, collector.schema_version, collected_at,
        time.monotonic() - t0, data, errors,
    )


def run_sources(
    sources: list[SourceConfig],
    query_fn: Callable[..., list[dict[str, Any]]] | None = None,
    max_workers: int = MAX_WORKERS,
    priors: dict[str, dict[str, Any]] | None = None,
    deep: bool = False,
) -> list[dict[str, Any]]:
    """Results in the same order as `sources`. `priors`: source name -> latest usable earlier result (heavy-section reuse)."""
    if not sources:
        return []
    pool = ThreadPoolExecutor(max_workers=max_workers)
    futures = [(s, pool.submit(run_source, s, query_fn, (priors or {}).get(s.name), deep)) for s in sources]
    results: list[dict[str, Any]] = []
    for s, fut in futures:
        try:
            results.append(fut.result(timeout=s.timeout_seconds + 30))
        except FutureTimeout:
            results.append(make_result(
                s.name, s.type, get_collector(s.type).schema_version, utc_iso(),
                float(s.timeout_seconds), {}, [f"source timed out after {s.timeout_seconds}s"],
            ))
    pool.shutdown(wait=False, cancel_futures=True)
    return results
