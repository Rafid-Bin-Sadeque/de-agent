"""`dea find`: SQLite FTS5 over hub pages + platform snapshots, tuned for exact identifiers (M1a section 9)."""
from __future__ import annotations

import re
import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".dea", "node_modules", "__pycache__"}
SPLIT_TR = str.maketrans({".": " ", "/": " ", "-": " ", ":": " "})
TERM_RE = re.compile(r"[A-Za-z0-9_]+")
SCHEMA = """
create virtual table if not exists docs using fts5(
  path unindexed, kind unindexed, title, body, body_split,
  tokenize = "unicode61 tokenchars '_'"
);
create table if not exists files(path text primary key, mtime_ns integer, size integer);
"""


@dataclass
class Hit:
    path: str
    line: int
    snippet: str
    kind: str
    score: float

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "snippet": self.snippet, "kind": self.kind,
                "score": self.score}


def fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("create virtual table t using fts5(x)")
        c.close()
        return True
    except sqlite3.Error:
        return False


def kind_of(rel: str) -> str:
    parts = rel.split("/")
    if len(parts) == 1:
        return {"map.md": "map", "status.md": "status", "conventions.md": "conventions"}.get(rel, "other")
    return {"domains": "domain", "projects": "project", "integrations": "integration",
            "platform": "platform", "catalog": "catalog"}.get(parts[0], "other")


def _title(rel: str, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return Path(rel).stem


def split_ident(text: str) -> str:
    return text.translate(SPLIT_TR)


def query_terms(q: str) -> list[str]:
    return TERM_RE.findall(split_ident(q))


class FindIndex:
    def __init__(self, hub: Path) -> None:
        self.hub = hub
        self.db_path = hub / ".dea" / "index.db"

    def _files(self) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        for p in self.hub.rglob("*.md"):
            rel = p.relative_to(self.hub)
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            found[rel.as_posix()] = (st.st_mtime_ns, st.st_size)
        return found

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.executescript(SCHEMA)
        return con

    def refresh(self, rebuild: bool = False) -> int:
        """Reindex changed files only; returns the number of files (re)indexed."""
        con = self._connect()
        try:
            if rebuild:
                con.execute("delete from docs")
                con.execute("delete from files")
            known = {r[0]: (r[1], r[2]) for r in con.execute("select path, mtime_ns, size from files")}
            current = self._files()
            gone = [p for p in known if p not in current]
            changed = [p for p, sig in current.items() if known.get(p) != sig]
            for p in gone + changed:
                con.execute("delete from docs where path = ?", (p,))
                con.execute("delete from files where path = ?", (p,))
            for rel in changed:
                try:
                    text = (self.hub / rel).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                con.execute("insert into docs(path, kind, title, body, body_split) values (?,?,?,?,?)",
                            (rel, kind_of(rel), _title(rel, text), text, split_ident(text)))
                con.execute("insert into files(path, mtime_ns, size) values (?,?,?)", (rel, *current[rel]))
            con.commit()
            return len(changed)
        finally:
            con.close()

    def search(self, query: str, limit: int = 10) -> list[Hit]:
        terms = query_terms(query)
        if not terms:
            return []
        con = self._connect()
        try:
            for joiner in (" AND ", " OR "):
                match = joiner.join(f'"{t}"' for t in terms)
                try:
                    rows = con.execute(
                        "select path, kind, bm25(docs, 0, 0, 5, 1, 1) as score from docs "
                        "where docs match ? order by score, path limit ?", (match, max(limit * 5, 50))).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows or len(terms) == 1:
                    break
        finally:
            con.close()
        rows = self._definitions_first(rows, query)[:limit]
        return [self._hit(r[0], r[1], r[2], terms, query) for r in rows]

    def _definitions_first(self, rows: list[tuple], query: str) -> list[tuple]:
        """Pages with a line that STARTS with the identifier (a catalog/edges entry) outrank prose that mentions it."""
        q = query.strip().lower()
        if not q or len(query_terms(query)) < 2 and "." not in q:
            return rows

        def defines(rel: str) -> bool:
            try:
                text = (self.hub / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return any(ln.lstrip("-* ").lower().startswith(q) for ln in text.splitlines() if q in ln.lower())

        return sorted(rows, key=lambda r: (0 if defines(r[0]) else 1, r[2], r[0]))

    def _hit(self, rel: str, kind: str, score: float, terms: list[str], query: str = "") -> Hit:
        line_no, snippet = first_matching_line(self.hub / rel, terms, query)
        return Hit(rel, line_no, snippet, kind, round(score, 3))


def _window(line: str, pos: int, width: int = 160) -> str:
    """The line itself when short, else a window of it around the match (long catalog lines)."""
    line = line.strip()
    if len(line) <= width:
        return line
    start = max(0, min(pos - 50, len(line) - width))
    return ("…" if start else "") + line[start:start + width]


def first_matching_line(path: Path, terms: list[str], query: str = "") -> tuple[int, str]:
    low = [t.lower() for t in terms]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 1, ""
    q = query.strip().lower()
    if q and len(terms) > 1:  # prefer the line that holds the whole identifier, e.g. `raw.orders`
        for i, line in enumerate(lines, 1):
            if q in line.lower():
                return i, _window(line, line.lower().index(q))
    for i, line in enumerate(lines, 1):
        hay = split_ident(line).lower() + " " + line.lower()
        if any(t in hay for t in low):
            hits = [line.lower().find(t) for t in low if line.lower().find(t) >= 0]
            return i, _window(line, min(hits) if hits else 0)
    return 1, (lines[0].strip()[:160] if lines else "")


def linear_search(hub: Path, query: str, limit: int = 10) -> list[Hit]:
    """Fallback when FTS5 is unavailable: count term occurrences per page."""
    warnings.warn("SQLite FTS5 is unavailable; falling back to a slow linear scan", stacklevel=2)
    terms = query_terms(query)
    scored: list[tuple[int, str, str]] = []
    for rel in sorted(FindIndex(hub)._files()):
        text = (hub / rel).read_text(encoding="utf-8", errors="replace")
        hay = (split_ident(text) + " " + text).lower()
        n = sum(hay.count(t.lower()) for t in terms)
        if n:
            scored.append((-n, rel, kind_of(rel)))
    scored.sort()
    out = []
    for neg, rel, kind in scored[:limit]:
        ln, snip = first_matching_line(hub / rel, terms, query)
        out.append(Hit(rel, ln, snip, kind, float(neg)))
    return out


def find(hub: Path, query: str, limit: int = 10, rebuild: bool = False) -> list[Hit]:
    if not fts5_available():
        return linear_search(hub, query, limit)
    idx = FindIndex(hub)
    idx.refresh(rebuild=rebuild)
    return idx.search(query, limit)



def find_index_refresh(hub: Path) -> int:
    """Incremental reindex (called by `dea scan`)."""
    return FindIndex(hub).refresh()
