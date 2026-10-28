"""HMAC-signed plan approvals (M2 section 1). Stdlib only and Python 3.9 compatible: the guard hooks import this.

Threat model (docs/SAFETY.md): the key file stops accidents and casual bypass by a model that cannot read it
(secret_guard) or run `dea approve` (approval_guard). It is not a sandbox against a hostile local process that runs as you.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile

KEY_NAME = "approve.key"


def key_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "de-agent", KEY_NAME)


def load_key(create=False):
    """Return the key bytes, or None when it does not exist (and create is False)."""
    path = key_path()
    try:
        with open(path, "rb") as f:
            data = f.read().strip()
        return data or None
    except OSError:
        pass
    if not create:
        return None
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    key = os.urandom(32).hex().encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key + b"\n")
    return key


def _canonical(payload):
    body = {
        "plan_id": payload.get("plan_id"),
        "hashes": payload.get("hashes"),
        "approver": payload.get("approver"),
        "approved_at": payload.get("approved_at"),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(payload, key):
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def make_approval(plan_id, hashes, approver, approved_at, key):
    data = {"plan_id": plan_id, "hashes": list(hashes), "approver": approver, "approved_at": approved_at}
    data["mac"] = sign(data, key)
    return data


def write_approval(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def verify_data(data, key, plan_id=None):
    """Return (ok, reason)."""
    if not isinstance(data, dict):
        return False, "approval file is not a JSON object"
    mac = data.get("mac")
    hashes = data.get("hashes")
    if not isinstance(mac, str) or not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes):
        return False, "approval file is malformed"
    if key is None:
        return False, "no approval key on this machine (run `dea approve` in your own terminal)"
    if not hmac.compare_digest(mac, sign(data, key)):
        return False, "bad MAC (approval file was edited, or signed with a different key)"
    if plan_id is not None and data.get("plan_id") != plan_id:
        return False, "approval is for a different plan id"
    return True, ""


def read_approval(path, key, plan_id=None):
    """Return (data-or-None, reason). data is returned only when the MAC verifies."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as ex:
        return None, f"unreadable approval file: {ex}"
    ok, why = verify_data(data, key, plan_id)
    return (data if ok else None), why


def approved_hashes(project_dir):
    """Union of hashes from every `.approved` file whose MAC verifies. Bad or unsigned files grant nothing."""
    import glob

    key = load_key()
    out = set()
    for path in glob.glob(os.path.join(project_dir, ".dea", "plans", "*.approved")):
        stem = os.path.basename(path)[: -len(".approved")]
        data, _ = read_approval(path, key, stem)
        if data:
            out.update(data["hashes"])
    return out
