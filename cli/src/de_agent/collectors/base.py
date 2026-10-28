"""Collector protocol and result shape (M1a section 4)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from de_agent.config import SourceConfig


class CollectorError(Exception):
    """A collector query was refused or failed. Messages never contain secrets."""


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunContext:
    """Per-source run budget. `timeout_seconds` is the total budget for the source."""

    timeout_seconds: float
    started: float = field(default_factory=time.monotonic)
    query_fn: Callable[..., list[dict]] | None = None  # injectable for tests
    aws_session: Any = None  # injectable boto3 session for tests (aws / redshift describe)
    prior: dict[str, Any] | None = None  # latest usable earlier result of this source (heavy-section reuse)
    deep: bool = False  # `dea snapshot --deep`: recompute everything, ignore `prior`
    now: datetime | None = None  # injectable clock (tests)

    def remaining(self) -> float:
        return self.timeout_seconds - (time.monotonic() - self.started)

    def query(self, source: SourceConfig, sql: str) -> list[dict]:
        from de_agent import sqlexec

        left = self.remaining()
        if left <= 0:
            raise CollectorError("source timeout budget exhausted")
        fn = self.query_fn or sqlexec.run_query
        return fn(source.conn, sql, source.exec, max(1, int(left)))


class Collector(Protocol):
    type: str
    schema_version: int
    list_keys: dict[str, list[str]]
    volatile_fields: set[str]

    def collect(self, source: SourceConfig, ctx: RunContext) -> dict[str, Any]:
        """Return {"data": {...}, "errors": [...]}; a failing section goes to errors, the rest stays."""
        ...


def make_result(
    source: str, ctype: str, schema_version: int, collected_at: str, duration_s: float,
    data: dict[str, Any], errors: list[str],
) -> dict[str, Any]:
    return {
        "source": source, "type": ctype, "schema_version": schema_version,
        "collected_at": collected_at, "duration_s": round(duration_s, 2),
        "data": data, "errors": errors,
    }
