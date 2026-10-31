"""HTTP fetching for `kind: http` packs, stdlib only (no de-agent imports).

Executed in two places: imported locally by collectors/pack_http.py, and shipped as text over `ssh <host> python3 -` for
`exec: ssh:<host>` sources (secrets are then resolved on the remote host from env:/file: refs). Keep it Python 3.8+.

`request()` is the ONLY function that touches the network, and it is read-only by construction: GET, or POST of a GraphQL
document that contains no mutation or subscription. Anything else raises ValueError before a socket is opened. Redirects are not
followed (an Authorization header must never travel to another host). Responses are capped at 5 MB.
"""
import base64
import json
import os
import re
import stat
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024 * 1024
MAX_GRAPHQL = 10000

_REDACT = [
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+(@)", re.I), r"\1***\2"),
    (re.compile(r"(\b(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s'\",&]+", re.I), r"\1***"),
    (re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1***"),
    (re.compile(r"(\bAuthorization:\s*(?:Basic|Bearer)\s+)\S+", re.I), r"\1***"),
]


def redact_text(text):
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

_WRITE_WORDS = re.compile(r"\b(mutation|subscription)\b")


def _graphql_bare(q):
    """`q` with comments blanked and string / block-string contents emptied, scanned left to right so that a `#` inside a
    string and a quote inside a comment are each read correctly. None when a string is not terminated."""
    out = []
    i, n = 0, len(q)
    while i < n:
        c = q[i]
        if c == "#":
            j = q.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
        elif q.startswith('"""', i):
            j = i + 3
            while j < n and not q.startswith('"""', j):
                j += 4 if q.startswith('\\"""', j) else 1
            if j >= n:
                return None
            out.append('""')
            i = j + 3
        elif c == '"':
            j = i + 1
            while j < n and q[j] not in '"\n':
                j += 2 if q[j] == "\\" else 1
            if j >= n or q[j] != '"':
                return None
            out.append('""')
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def graphql_refusal(query):
    """None when `query` is a read-only GraphQL document (starts with `query` or `{`; no mutation/subscription anywhere)."""
    if not isinstance(query, str) or not query.strip():
        return "graphql query is empty"
    if len(query) > MAX_GRAPHQL:
        return "graphql query is too long"
    bare = _graphql_bare(query)
    if bare is None:
        return "graphql query has a string that is not terminated"
    head = bare.lstrip()
    if not (head.startswith("{") or re.match(r"query\b", head)):
        return "a graphql document must start with `query` or `{` (never mutation or subscription)"
    if _WRITE_WORDS.search(bare):
        return "graphql documents may not contain mutation or subscription"
    return None


def path_refusal(path):
    """None when `path` is a relative request path (`/v1/things?limit=5`) that stays on the source's own host."""
    if not isinstance(path, str) or not path:
        return "path is empty"
    if not path.startswith("/") or path.startswith("//"):
        return "path must start with a single '/' (it is relative to the source's base_url)"
    if re.search(r"[\s\x00-\x1f\\]", path) or "://" in path:
        return "path may not contain whitespace, backslashes or a scheme"
    if ".." in path.split("?", 1)[0].split("/"):
        return "path may not contain '..' segments"
    return None


def build_auth_header(auth, user, secret, header_name):
    """(header name, value) or None."""
    if auth == "none" or secret is None:
        return None
    if auth == "bearer":
        return ("Authorization", "Bearer " + secret)
    if auth == "basic":
        raw = ("%s:%s" % (user or "", secret)).encode("utf-8")
        return ("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
    if auth == "header":
        return (header_name or "X-Api-Key", secret)
    raise ValueError("unknown auth %r" % (auth,))


def request(base_url, method, path, headers, timeout, graphql_query=None):
    """One read-only request. Returns (status, parsed JSON body). Raises ValueError (refused) or IOError (failed)."""
    why = path_refusal(path)
    if why:
        raise ValueError(why)
    if method == "GET":
        if graphql_query is not None:
            raise ValueError("GET does not carry a graphql document")
        data = None
    elif method == "POST":
        why = graphql_refusal(graphql_query)
        if why:
            raise ValueError(why)
        data = json.dumps({"query": graphql_query}).encode("utf-8")
    else:
        raise ValueError("only GET and read-only GraphQL POST are allowed (got %s)" % (method,))
    base = urlsplit(base_url)
    if base.scheme not in ("http", "https") or not base.netloc:
        raise ValueError("base_url must be an http(s) URL")
    url = base_url.rstrip("/") + path
    if urlsplit(url).netloc != base.netloc:
        raise ValueError("request would leave the source's host")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as ex:
        return ex.code, None
    except (urllib.error.URLError, OSError, ValueError) as ex:
        raise OSError(redact_text("request failed: %s" % (getattr(ex, "reason", None) or type(ex).__name__))) from None
    try:
        raw = resp.read(MAX_BYTES + 1)
        status = getattr(resp, "status", 200)
    finally:
        resp.close()
    if len(raw) > MAX_BYTES:
        raise OSError("response larger than 5 MB")
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError:
        raise OSError("response was not JSON") from None


def fetch(base_url, reqs, headers, timeout):
    """reqs: [{"kind": "get"|"graphql", "path": str, "query": str|None}] -> [{"ok": True, "body": ...} | {"ok": False, "error": str}]."""
    out = []
    for r in reqs:
        try:
            if r.get("kind") == "get":
                status, body = request(base_url, "GET", r["path"], headers, timeout)
            elif r.get("kind") == "graphql":
                status, body = request(base_url, "POST", r["path"], headers, timeout, r.get("query"))
            else:
                raise ValueError("only GET and read-only GraphQL POST are allowed")
        except (OSError, ValueError) as ex:
            out.append({"ok": False, "error": redact_text(str(ex))})
            continue
        if status is None or status >= 300 or body is None:
            out.append({"ok": False, "error": "HTTP %s" % (status,)})
        else:
            out.append({"ok": True, "body": body})
    return out


def _resolve_remote(ref):
    if ref is None or ref == "none":
        return None
    if ref.startswith("env:"):
        val = os.environ.get(ref[4:])
        if val is None:
            raise ValueError("environment variable %s is not set on the remote host" % ref[4:])
        return val
    if ref.startswith("file:"):
        path = ref[5:]
        try:
            if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                raise ValueError("secret file %s must be mode 0600 or stricter" % path)
            with open(path) as f:
                return f.read().strip()
        except OSError as ex:
            raise ValueError("cannot read secret file %s: %s" % (path, ex.strerror)) from None
    raise ValueError("secret refs over ssh must be env: or file: (resolved on the remote host)")


def remote_main(cfg):
    """cfg: {base_url, auth, user, secret_ref, header_name, timeout, requests}. Secrets are resolved HERE, on the remote host."""
    try:
        secret = _resolve_remote(cfg.get("secret_ref"))
        hdr = build_auth_header(cfg.get("auth", "none"), cfg.get("user"), secret, cfg.get("header_name"))
    except ValueError as ex:
        return {"error": redact_text(str(ex))}
    headers = {hdr[0]: hdr[1]} if hdr else {}
    return {"results": fetch(cfg["base_url"], cfg["requests"], headers, cfg.get("timeout", 20))}
