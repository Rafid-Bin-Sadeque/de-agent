"""Airflow REST gathering, stdlib only (no de-agent imports).

This file is executed in two places: imported locally by collectors/airflow.py, and shipped as text over
`ssh <host> python3 -` for `exec: ssh:<host>` sources. Keep it Python 3.8+ and stdlib-only.
`get(path, params, accept)` returns (status, body); it is the only thing that touches the network, so it is
GET-only by construction and replaced by a replay function in tests.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

LOG_TAIL_LINES = 40
STACK_LINES = 5
MAX_FAILURES = 50
MAX_LOG_FETCHES = 20
PAGE = 100

_REDACT = [
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+(@)", re.I), r"\1***\2"),
    (re.compile(r"(\b(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s'\",&]+", re.I), r"\1***"),
    (re.compile(r"\b((?:PGPASSWORD|MYSQL_PWD)=)\S+"), r"\1***"),
    (re.compile(r"(\bAWS_SECRET_ACCESS_KEY\s*[=:]\s*[\"']?)[A-Za-z0-9/+=]{20,}"), r"\1***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1***"),
    (re.compile(r"(\bAuthorization:\s*(?:Basic|Bearer)\s+)\S+", re.I), r"\1***"),
]


def redact_text(text):
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


def _parse_ts(v):
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class ApiError(Exception):
    pass


def _json(get, path, params=None):
    status, body = get(path, params, "application/json")
    if status != 200 or not isinstance(body, (dict, list)):
        raise ApiError("GET %s -> HTTP %s" % (path, status))
    return body


def _paged(get, path, key, params, cap):
    out, offset = [], 0
    while len(out) < cap:
        body = _json(get, path, list(params) + [("limit", PAGE), ("offset", offset)])
        rows = body.get(key) or []
        out += rows
        total = body.get("total_entries")
        offset += PAGE
        if not rows or (total is not None and offset >= total):
            break
    return out[:cap]


def detect_api(get):
    net_error = None
    for base in ("/api/v2", "/api/v1"):
        try:
            status, body = get(base + "/version", None, "application/json")
        except ApiError as ex:  # network failure: remember it, it is a better message than "no API found"
            net_error = ex
            continue
        if status == 200 and isinstance(body, dict) and body.get("version"):
            return base, str(body["version"])
    if net_error is not None:
        raise net_error
    raise ApiError("no Airflow API found at /api/v2/version or /api/v1/version")


# ---- normalisers (Airflow 2.x /api/v1 and 3.x /api/v2 differ in field names) --------------------------

def _dag_row(d):
    si = d.get("schedule_interval")
    expr = si.get("value") if isinstance(si, dict) else (si if isinstance(si, str) else None)
    expr = expr or d.get("timetable_summary")
    nxt = d.get("next_dagrun_create_after") or d.get("next_dagrun_run_after") or d.get("next_dagrun")
    return {
        "dag_id": d.get("dag_id"),
        "is_paused": bool(d.get("is_paused")),
        "schedule": expr,
        "schedule_desc": d.get("timetable_description"),
        "tags": sorted(t.get("name") if isinstance(t, dict) else str(t) for t in d.get("tags") or []),
        "owners": ",".join(d.get("owners") or []) if isinstance(d.get("owners"), list) else d.get("owners"),
        "fileloc": d.get("fileloc"),
        "next_dagrun": nxt,
        "has_import_errors": bool(d.get("has_import_errors")),
    }


def _is_live(d):
    if "is_stale" in d:
        return not d["is_stale"]
    return d.get("is_active", True) is not False


def _run_row(dag_id, r):
    start, end = _parse_ts(r.get("start_date")), _parse_ts(r.get("end_date"))
    return {
        "dag_id": dag_id,
        "run_id": r.get("dag_run_id"),
        "state": r.get("state"),
        "logical_date": r.get("logical_date") or r.get("execution_date"),
        "start": _iso(start),
        "end": _iso(end),
        "duration_s": int((end - start).total_seconds()) if start and end else None,
    }


def _log_lines(body):
    if isinstance(body, str):
        return body.splitlines()
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, str):
            return content.splitlines()
        lines = []
        for item in content or []:
            if isinstance(item, dict):
                lines.append(str(item.get("event", item)))
            elif isinstance(item, (list, tuple)) and item:
                lines.append(str(item[-1]))
            else:
                lines.append(str(item))
        return lines
    return []


def _log_tail(get, api, ti):
    path = "%s/dags/%s/dagRuns/%s/taskInstances/%s/logs/%s" % (
        api, quote(ti["dag_id"], safe=""), quote(ti["dag_run_id"], safe=""),
        quote(ti["task_id"], safe=""), int(ti.get("try_number") or 1))
    status, body = get(path, [("full_content", "true")], "text/plain")
    if status != 200:
        raise ApiError("log HTTP %s" % status)
    lines = [ln.rstrip() for ln in _log_lines(body) if ln.strip()]
    return redact_text("\n".join(lines[-LOG_TAIL_LINES:]))


def schedule_interval_s(schedule, runs):
    """Best-effort interval in seconds: cron/preset heuristics, else the median gap of scheduled runs."""
    s = (schedule or "").strip()
    presets = {"@hourly": 3600, "@daily": 86400, "@weekly": 604800, "@monthly": 2592000, "@yearly": 31536000,
               "@once": None, "@continuous": None}
    if s in presets:
        return presets[s]
    parts = s.split()
    if len(parts) == 5:
        minute, hour, dom, mon, dow = parts
        if hour == "*" and dom == mon == dow == "*" and not minute.startswith("*/"):
            return 3600
        if minute.startswith("*/") and minute[2:].isdigit():
            return int(minute[2:]) * 60
        if hour.isdigit() and dom == mon == dow == "*":
            return 86400
        if dow != "*" and dom == mon == "*":
            return 604800
    scheduled = sorted(t for t in (_parse_ts(r.get("logical_date")) for r in runs
                                   if str(r.get("run_id", "")).startswith("scheduled__")) if t)
    gaps = sorted((b - a).total_seconds() for a, b in zip(scheduled, scheduled[1:]))
    return int(gaps[len(gaps) // 2]) if gaps else None


def gather(cfg, get, now=None):
    """Return {"data": {...}, "errors": [...]}; a failing section is reported, the others still return."""
    now = now or datetime.now(timezone.utc)
    max_dags = int(cfg.get("max_dags", 500))
    runs_per_dag = int(cfg.get("runs_per_dag", 5))
    fetch_logs = bool(cfg.get("fetch_logs", True))
    data, errors = {}, []
    api, version = detect_api(get)
    data["version"] = {"version": version, "api": api.rsplit("/", 1)[1]}
    is_v2 = api.endswith("v2")

    def section(name, fn):
        try:
            data[name] = fn()
        except ApiError as ex:
            errors.append("%s: %s" % (name, ex))

    def dags():
        raw = _paged(get, api + "/dags", "dags", [("order_by", "dag_id")], max_dags)
        return [_dag_row(d) for d in raw if _is_live(d)]

    section("dags", dags)
    dag_ids = [d["dag_id"] for d in data.get("dags", [])]

    def runs():
        out = []
        for dag_id in dag_ids:
            body = _json(get, "%s/dags/%s/dagRuns" % (api, quote(dag_id, safe="")),
                         [("order_by", "-start_date"), ("limit", runs_per_dag)])
            out += [_run_row(dag_id, r) for r in body.get("dag_runs") or []]
        return out

    if "dags" in data:
        section("runs", runs)

    def failures():
        since = _iso(now - timedelta(hours=24))
        tis = _paged(get, api + "/dags/~/dagRuns/~/taskInstances", "task_instances",
                     [("state", "failed"), ("state", "up_for_retry"), ("start_date_gte", since)], MAX_FAILURES)
        tis.sort(key=lambda t: str(t.get("start_date")), reverse=True)  # the endpoint has no order_by
        out = []
        for i, ti in enumerate(tis):
            row = {"dag_id": ti.get("dag_id"), "task_id": ti.get("task_id"), "try": int(ti.get("try_number") or 1),
                   "run_id": ti.get("dag_run_id"), "state": ti.get("state"), "start": ti.get("start_date"),
                   "duration_s": int(ti["duration"]) if isinstance(ti.get("duration"), (int, float)) else None}
            if fetch_logs and i < MAX_LOG_FETCHES:
                try:
                    row["log_tail"] = _log_tail(get, api, ti)
                except ApiError as ex:
                    row["log_tail"] = "(log unavailable: %s)" % ex
            out.append(row)
        return out

    section("failures_24h", failures)

    def import_errors():
        rows = _paged(get, api + "/importErrors", "import_errors", [], 100)
        return [{"filename": r.get("filename"),
                 "stack": "\n".join((r.get("stack_trace") or "").splitlines()[:STACK_LINES])} for r in rows]

    section("import_errors", import_errors)

    def datasets():
        key = "assets" if is_v2 else "datasets"
        rows = _paged(get, "%s/%s" % (api, key), key, [], 200)
        out = []
        for r in rows:
            last = None
            le = r.get("last_asset_event") or r.get("last_dataset_event")
            if isinstance(le, dict):
                last = le.get("timestamp")
            if last is None:
                ev = "assets/events" if is_v2 else "datasets/events"
                idk = "asset_id" if is_v2 else "dataset_id"
                evk = "asset_events" if is_v2 else "dataset_events"
                try:
                    body = _json(get, "%s/%s" % (api, ev), [(idk, r.get("id")), ("order_by", "-timestamp"), ("limit", 1)])
                    evs = body.get(evk) or []
                    last = evs[0].get("timestamp") if evs else None
                except ApiError:
                    last = None
            prod = sorted("%s.%s" % (t.get("dag_id"), t.get("task_id")) for t in r.get("producing_tasks") or [])
            cons = sorted(set(c.get("dag_id") for c in (r.get("consuming_dags") or []) if isinstance(c, dict)))
            out.append({"uri": r.get("uri") or r.get("name"), "producing_tasks": prod, "consuming_dags": cons,
                        "last_event": _iso(_parse_ts(last))})
        return out

    section("datasets", datasets)

    def pools():
        body = _json(get, api + "/pools", [("limit", PAGE)])
        return [{"name": p.get("name"), "slots": p.get("slots"), "occupied": p.get("occupied_slots"),
                 "queued": p.get("queued_slots")} for p in body.get("pools") or []]

    section("pools", pools)
    data["meta"] = {"collected_at": _iso(now), "runs_per_dag": runs_per_dag, "max_dags": max_dags,
                    "fetch_logs": fetch_logs}
    return {"data": data, "errors": errors}


# ---- remote entry point (exec: ssh:<host>) ------------------------------------------------------------

def resolve_remote_secret(ref):
    import os
    if ref.startswith("env:"):
        val = os.environ.get(ref[4:])
        if val is None:
            raise ApiError("environment variable %s is not set on the remote host" % ref[4:])
        return val
    if ref.startswith("file:"):
        try:
            with open(ref[5:]) as fh:
                return fh.read().strip()
        except OSError as ex:
            raise ApiError("cannot read %s on the remote host: %s" % (ref[5:], ex.strerror)) from None
    raise ApiError("on a remote host the secret ref must be env: or file:")


def make_urllib_get(base_url, auth_header, timeout=20):
    import urllib.error
    import urllib.request

    def get(path, params=None, accept="application/json"):
        from urllib.parse import urlencode
        url = base_url.rstrip("/") + path + ("?" + urlencode(params) if params else "")
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", accept)
        if auth_header:
            req.add_header("Authorization", auth_header)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw, status, ctype = resp.read(), resp.status, resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as ex:
            raw, status, ctype = ex.read(), ex.code, ex.headers.get("Content-Type", "")
        except (urllib.error.URLError, OSError) as ex:
            raise ApiError("cannot reach %s: %s" % (base_url, getattr(ex, "reason", ex))) from None
        text = raw.decode("utf-8", "replace")
        if "json" in ctype:
            try:
                return status, json.loads(text)
            except ValueError:
                return status, text
        return status, text

    return get


def auth_header_for(cfg, secret):
    import base64
    if cfg.get("auth") == "token":
        return "Bearer " + secret
    return "Basic " + base64.b64encode(("%s:%s" % (cfg.get("user", ""), secret)).encode()).decode()


def remote_main(cfg):
    """Entry point used on the remote host: resolve the secret THERE, call the API, return the result dict."""
    try:
        ref = cfg.get("token") if cfg.get("auth") == "token" else cfg.get("password")
        secret = resolve_remote_secret(ref) if ref else None
        get = make_urllib_get(cfg["base_url"], auth_header_for(cfg, secret) if secret else None)
        return gather(cfg, get)
    except ApiError as ex:
        return {"data": {}, "errors": [str(ex)]}
