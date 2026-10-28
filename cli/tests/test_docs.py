"""Docs stay true: generated config reference does not drift, examples parse, relative links resolve."""
import re
from pathlib import Path

import pytest
from de_agent import configdoc
from de_agent.config import parse_config

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"(?<!\!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"```.*?```", re.S)


def test_configuration_md_matches_models():
    assert (ROOT / "docs" / "configuration.md").read_text() == configdoc.markdown(), \
        "docs/configuration.md drifted: dea config schema --markdown -o docs/configuration.md"


def test_json_schema_matches_models():
    assert (ROOT / "docs" / "de-agent.schema.json").read_text() == configdoc.json_text(), \
        "docs/de-agent.schema.json drifted: dea config schema -o docs/de-agent.schema.json"


def test_config_doc_example_is_valid():
    block = re.search(r"```yaml\n(version: 1.*?)```", configdoc.HEAD, re.S).group(1)
    cfg = parse_config(block)
    assert cfg.sources[0].name == "warehouse" and cfg.guard.mode == "strict"


def test_every_config_field_is_documented():
    schema = configdoc.json_schema()
    for model in [schema, *schema["$defs"].values()]:
        for key, node in model.get("properties", {}).items():
            assert node.get("description"), f"{key} has no description"


def _markdown_files():
    skip = ("evals/results", "cli/tests/", "demo/", "cli/.venv", "node_modules", ".git/")
    for p in sorted(ROOT.rglob("*.md")):
        rel = p.relative_to(ROOT).as_posix()
        if not any(rel.startswith(s) or f"/{s}" in f"/{rel}" for s in skip):
            yield p


@pytest.mark.parametrize("md", list(_markdown_files()), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(md):
    text = FENCE.sub("", md.read_text(encoding="utf-8"))
    bad = []
    for target in LINK.findall(text):
        if re.match(r"^(?:[a-z]+:|#)", target):
            continue
        path = target.split("#", 1)[0]
        if path and not (md.parent / path).exists():
            bad.append(target)
    assert not bad, f"{md.relative_to(ROOT)}: broken links {bad}"
