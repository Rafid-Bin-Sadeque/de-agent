"""HTTP packs against a local server: GET and GraphQL queries, auth, no redirects, size cap, ssh remote script with remote secret."""
import http.server
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone

import pytest
from de_agent.collectors import _pack_http_core as core
from de_agent.collectors import pack_http
from de_agent.config import parse_config
from de_agent.packs.loader import set_hub_hint
from de_agent.runner import result_status, run_source
from packs_helpers import install

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
REAL_RUN = subprocess.run


class Handler(http.server.BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        Handler.log.append(("GET", self.path, self.headers.get("Authorization"), self.headers.get("X-Api-Key")))
        if self.path.startswith("/v1/groups/g1/connectors"):
            self._send(200, {"data": {"items": [{"id": "c1", "schema": "s1", "service": "pg", "paused": False,
                                                 "status": {"sync_state": "scheduled", "setup_state": "connected"},
                                                 "succeeded_at": "2026-10-01T10:00:00Z", "failed_at": None}]}})
        elif self.path == "/redirect":
            self._send(302, b"", {"Location": "http://evil.invalid/steal"})
        elif self.path == "/huge":
            self._send(200, b"[" + b"1," * (3 * 1024 * 1024) + b"1]")
        elif self.path == "/text":
            self._send(200, b"not json")
        elif self.path == "/err":
            self._send(500, {"error": "boom"})
        else:
            self._send(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        Handler.log.append(("POST", self.path, self.headers.get("Authorization"), self.headers.get("X-Api-Key"), body["query"][:30]))
        self._send(200, {"data": {"runsOrError": {"results": [{"runId": "r1", "jobName": "j", "status": "FAILURE", "startTime": 1.0, "endTime": 2.0}]}}})

    def do_DELETE(self):
        Handler.log.append(("DELETE", self.path))
        self._send(200, {})


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    Handler.log.clear()
    install(tmp_path)
    yield
    set_hub_hint(None)


def src(base, pack="fttest", auth="basic", extra="", options='{group_ids: [g1]}'):
    return parse_config(
        f"sources:\n  - name: ft\n    type: http\n    pack: {pack}\n    base_url: '{base}'\n    auth: {auth}\n{extra}\n    options: {options}\n").sources[0]


def test_get_pack_end_to_end_with_basic_auth(server, monkeypatch):
    monkeypatch.setenv("DEA_T_PW", "s3cret")
    res = run_source(src(server, extra="    user: apikey\n    password: 'env:DEA_T_PW'"))
    assert result_status(res) == "ok", res["errors"]
    assert res["data"]["connectors"][0]["id"] == "c1" and res["data"]["connectors"][0]["group_id"] == "g1"
    method, path, auth, _ = Handler.log[0]
    assert method == "GET" and path == "/v1/groups/g1/connectors?limit=100"
    import base64

    assert auth == "Basic " + base64.b64encode(b"apikey:s3cret").decode()
    assert "s3cret" not in json.dumps(res)


def test_bearer_and_header_auth(server, monkeypatch):
    monkeypatch.setenv("DEA_T_TOKEN", "tok123")
    run_source(src(server, auth="bearer", extra="    token: 'env:DEA_T_TOKEN'"))
    assert Handler.log[0][2] == "Bearer tok123"
    Handler.log.clear()
    run_source(src(server, auth="header", extra="    token: 'env:DEA_T_TOKEN'\n    header_name: X-Api-Key"))
    assert Handler.log[0][3] == "tok123" and Handler.log[0][2] is None


def test_graphql_pack_posts_a_query_only(server):
    res = run_source(src(server, pack="dgtest", auth="none", options="{}"))
    assert result_status(res) in ("ok", "partial")
    posts = [e for e in Handler.log if e[0] == "POST"]
    assert posts and all(p[1] == "/graphql" and p[4].lstrip().startswith("query") for p in posts)
    assert res["data"]["failed_runs_24h"][0]["run_id"] == "r1"
    assert not [e for e in Handler.log if e[0] in ("DELETE", "PUT", "PATCH")]


def test_redirects_are_not_followed_and_the_token_never_leaves(server, monkeypatch):
    monkeypatch.setenv("DEA_T_TOKEN", "tok123")
    s = src(server, auth="bearer", extra="    token: 'env:DEA_T_TOKEN'")
    hdr = pack_http.HttpPackCollector()._headers(s)
    out = core.fetch(server, [{"kind": "get", "path": "/redirect"}], hdr, 5)
    assert out == [{"ok": False, "error": "HTTP 302"}]
    assert [e[1] for e in Handler.log] == ["/redirect"]  # evil.invalid was never contacted


@pytest.mark.parametrize("path,expect", [("/huge", "larger than 5 MB"), ("/text", "not JSON"), ("/err", "HTTP 500"), ("/missing", "HTTP 404")])
def test_http_failures_are_errors_not_crashes(server, path, expect):
    out = core.fetch(server, [{"kind": "get", "path": path}], {}, 5)
    assert out[0]["ok"] is False and expect in out[0]["error"]


def test_connection_refused_is_a_clean_error():
    out = core.fetch("http://127.0.0.1:9", [{"kind": "get", "path": "/x"}], {}, 2)
    assert out[0]["ok"] is False and "request failed" in out[0]["error"]


def test_delete_never_reaches_the_server(server):
    with pytest.raises(ValueError, match="only GET"):
        core.request(server, "DELETE", "/v1/anything", {}, 5)
    assert not [e for e in Handler.log if e[0] == "DELETE"]


def test_secrets_are_redacted_in_error_text():
    assert "tok123" not in core.redact_text("Authorization: Bearer tok123abcdefgh failed; password=hunter2")
    assert "hunter2" not in core.redact_text("password=hunter2")


def test_over_ssh_the_remote_script_resolves_the_secret_remotely(server, monkeypatch):
    """`exec: ssh:<host>` ships the stdlib core over `ssh host python3 -`; the secret ref is resolved there, never locally."""
    monkeypatch.delenv("DEA_REMOTE_TOKEN", raising=False)  # the LOCAL side never has the secret
    sent = {}

    def fake_run(argv, input=None, **kw):
        sent["argv"], sent["script"] = argv, input
        assert argv[0] == "ssh" and argv[-2:] == ["python3", "-"]
        p = REAL_RUN([sys.executable, "-"], input=input, capture_output=True, text=True, env={**__import__("os").environ, "DEA_REMOTE_TOKEN": "remote-only-secret"})  # only the "remote" process has it
        return p

    monkeypatch.setattr(pack_http.subprocess, "run", fake_run)
    s = src(server, auth="bearer", extra="    exec: 'ssh:bastion'\n    token: 'env:DEA_REMOTE_TOKEN'")
    res = run_source(s)
    assert result_status(res) == "ok", res["errors"]
    assert "remote-only-secret" not in sent["script"]  # only the reference is shipped
    assert "env:DEA_REMOTE_TOKEN" in sent["script"]
    assert Handler.log[0][2] == "Bearer remote-only-secret"


def test_over_ssh_a_missing_remote_secret_is_a_clean_error(server, monkeypatch):
    def fake_run(argv, input=None, **kw):
        return REAL_RUN([sys.executable, "-"], input=input, capture_output=True, text=True,
                              env={k: v for k, v in __import__("os").environ.items() if k != "DEA_NOPE"})

    monkeypatch.setattr(pack_http.subprocess, "run", fake_run)
    s = src(server, auth="bearer", extra="    exec: 'ssh:bastion'\n    token: 'env:DEA_NOPE'")
    res = run_source(s)
    assert result_status(res) == "failed" and "DEA_NOPE" in res["errors"][0]


def test_http_config_validation():
    for bad, msg in [
        ("{name: a, type: http, pack: fttest, auth: none}", "base_url"),
        ("{name: a, type: http, pack: fttest, base_url: 'ftp://x', auth: none}", "base_url"),
        ("{name: a, type: http, pack: fttest, base_url: 'http://x', auth: basic, options: {group_ids: [g]}}", "user"),
        ("{name: a, type: http, pack: fttest, base_url: 'http://x', auth: bearer, options: {group_ids: [g]}}", "token"),
        ("{name: a, type: http, pack: fttest, base_url: 'http://x', auth: header, token: 'env:X', options: {group_ids: [g]}}", "header_name"),
        ("{name: a, type: http, pack: fttest, base_url: 'http://x', auth: bearer, token: literal-secret, options: {group_ids: [g]}}", "secret literal"),
        ("{name: a, type: http, pack: fttest, base_url: 'http://x', auth: bearer, exec: 'ssh:h', token: pgpass, options: {group_ids: [g]}}", "env: or file:"),
        ("{name: a, type: http, pack: fttest, base_url: 'http://x', auth: none}", "group_ids"),
        ("{name: a, type: http, pack: fttest, base_url: 'http://x', auth: none, options: {group_ids: [g], bogus: 1}}", "bogus"),
        ("{name: a, type: http, pack: mytest, base_url: 'http://x', auth: none}", "kind sql"),
        ("{name: a, type: http, pack: nonexistent, base_url: 'http://x', auth: none}", "no pack named"),
        ("{name: a, type: http, base_url: 'http://x', auth: none}", "pack: <name>"),
    ]:
        with pytest.raises(Exception, match=msg):
            parse_config(f"sources:\n  - {bad}\n")
