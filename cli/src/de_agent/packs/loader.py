"""Find packs: hub-local `packs/<name>.yaml` overrides a built-in of the same name (package data)."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from de_agent.packs.errors import PackError
from de_agent.packs.model import NAME_RE, Pack, parse_pack

_hub_hint: Path | None = None


def set_hub_hint(hub: Path | None) -> None:
    """`load_config` tells us which hub it is loading, so hub-local packs resolve even when the cwd is elsewhere."""
    global _hub_hint
    _hub_hint = Path(hub) if hub is not None else None


def hub_packs_dir() -> Path:
    """`<hub>/packs`: the hub named by load_config, else $DEA_HUB / the nearest de-agent.yaml upward, else ./packs."""
    from de_agent.hubpath import find_hub

    base = _hub_hint if _hub_hint is not None and _hub_hint.is_dir() else (find_hub() or Path.cwd())
    return base / "packs"


@dataclass
class PackRef:
    name: str
    source: str  # "built-in" | "hub"
    text: str
    path: str  # file path (hub) or package resource name (built-in)

    def parse(self) -> Pack:
        return parse_pack(self.text)


def builtin_names() -> list[str]:
    root = resources.files("de_agent.packs")
    return sorted(p.name[:-5] for p in root.iterdir() if p.name.endswith(".yaml"))


def _builtin(name: str) -> PackRef | None:
    res = resources.files("de_agent.packs") / f"{name}.yaml"
    try:
        return PackRef(name, "built-in", res.read_text(encoding="utf-8"), f"de_agent/packs/{name}.yaml")
    except (FileNotFoundError, OSError):
        return None


def _hub(name: str) -> PackRef | None:
    p = hub_packs_dir() / f"{name}.yaml"
    try:
        return PackRef(name, "hub", p.read_text(encoding="utf-8"), str(p))
    except OSError:
        return None


def find_pack(name: str) -> PackRef:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PackError(f"invalid pack name {name!r}")
    ref = _hub(name) or _builtin(name)
    if ref is None:
        raise PackError(f"no pack named {name!r} (see `dea pack list`)")
    return ref


def load_pack(name: str) -> Pack:
    ref = find_pack(name)
    pack = ref.parse()
    if pack.pack != name:
        raise PackError(f"{ref.path}: file defines pack {pack.pack!r} but is named {name!r}")
    return pack


def list_packs() -> list[PackRef]:
    """Every pack visible from here: built-ins, with same-named hub files replacing them, plus hub-only packs."""
    refs: dict[str, PackRef] = {n: r for n in builtin_names() if (r := _builtin(n))}
    d = hub_packs_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.yaml")):
            if NAME_RE.match(p.stem):
                refs[p.stem] = PackRef(p.stem, "hub", p.read_text(encoding="utf-8"), str(p))
    return [refs[k] for k in sorted(refs)]
