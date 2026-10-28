"""`dea init`: scaffold a hub from the bundled templates (M1a section 10). Never overwrites."""
from __future__ import annotations

from datetime import date
from pathlib import Path

TEMPLATES = Path(__file__).parent / "templates"
# (template path under templates/hub, destination in the hub)
FILES = [
    ("de-agent.yaml", "de-agent.yaml"),
    ("map.md", "map.md"),
    ("status.md", "status.md"),
    ("conventions.md", "conventions.md"),
    ("README.md", "README.md"),
    ("domains/_template.md", "domains/_template.md"),
    ("projects/_template.md", "projects/_template.md"),
    ("integrations/_template.md", "integrations/_template.md"),
    ("gitignore.tmpl", ".gitignore"),
]
EXAMPLE_FILES = ["projects/orders_etl.md", "integrations/orders_etl--warehouse.md"]


def _fill(text: str, name: str, rows: str) -> str:
    name = name.replace('"', "'")
    text = text.replace("{{name}}", name).replace("{{today}}", date.today().isoformat())
    return text.replace("{{rows}}\n", rows + "\n" if rows else "")


def init_hub(target: Path, name: str, with_example: bool = False) -> tuple[list[str], list[str]]:
    """Create the hub; returns (created, skipped) relative paths."""
    target.mkdir(parents=True, exist_ok=True)
    rows = (TEMPLATES / "example" / "map_row.txt").read_text().strip() if with_example else ""
    plan: list[tuple[str, str]] = []
    for src, dst in FILES:
        plan.append((dst, _fill((TEMPLATES / "hub" / src).read_text(), name, rows)))
    plan.append(("platform/.gitkeep", ""))
    if with_example:
        for rel in EXAMPLE_FILES:
            plan.append((rel, _fill((TEMPLATES / "example" / rel).read_text(), name, "")))
    created, skipped = [], []
    for rel, content in plan:
        dest = target / rel
        if dest.exists():
            skipped.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        created.append(rel)
    return created, skipped
