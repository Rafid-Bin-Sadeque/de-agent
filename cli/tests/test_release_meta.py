"""Release metadata stays in step: one version everywhere, bundled licence files identical, no placeholder status."""
import json
import re
from pathlib import Path

import de_agent

ROOT = Path(__file__).resolve().parents[2]


def test_versions_agree():
    py = re.search(r'^version = "([^"]+)"', (ROOT / "cli" / "pyproject.toml").read_text(), re.M).group(1)
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert py == plugin == de_agent.__version__


def test_marketplace_names_the_plugin():
    mk = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert [p["name"] for p in mk["plugins"]] == ["de-agent"]


def test_licence_files_in_cli_match_the_root():
    for name in ("LICENSE", "NOTICE"):
        assert (ROOT / "cli" / name).read_text() == (ROOT / name).read_text()


def test_changelog_has_the_version_and_readme_is_not_pre_alpha():
    assert f"## [{de_agent.__version__}]" in (ROOT / "CHANGELOG.md").read_text()
    assert "pre-alpha" not in (ROOT / "README.md").read_text().lower()


def test_packaged_sql_corpus_exists():
    assert (Path(de_agent.__file__).parent / "data" / "sql_cases.yaml").is_file()
