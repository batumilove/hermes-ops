#!/usr/bin/env python3
"""Nous inference auth-proxy for Honcho embedding.
Serves /v1/embeddings (and liveness) on 127.0.0.1:18089, forwarding to
inference-api.nousresearch.com with the live JWT from Hermes' shared auth store.
NEVER refreshes tokens itself (Nous RTs are single-use; Hermes keepalive owns refresh).
"""

import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
import sqlite3
import uuid
import re
from pathlib import Path
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTH_PATH = os.environ.get(
    "NOUS_AUTH_PATH", "/home/ubuntu/.hermes/shared/nous_auth.json"
)
UPSTREAM = "https://inference-api.nousresearch.com"
PORT = int(os.environ.get("NOUS_PROXY_PORT", "18089"))
TTL = 30.0

_lock = threading.Lock()
_cache = {"token": None, "ts": 0.0, "exp": 0.0}


def live_token():
    with _lock:
        now = time.time()
        if _cache["token"] and now - _cache["ts"] < TTL and now < _cache["exp"] - 120:
            return _cache["token"]
        try:
            a = json.load(open(AUTH_PATH))
            tok = a["access_token"]
            exp = a.get("expires_at")
            # parse exp defensively; if unknown assume 5 min
            if exp:
                try:
                    import datetime

                    dt = datetime.datetime.fromisoformat(
                        exp.replace("+00:00", "+00:00")
                    )
                    exp_ts = dt.timestamp()
                except Exception:
                    exp_ts = now + 300
            else:
                exp_ts = now + 300
            if now > exp_ts - 120:
                sys.stderr.write(
                    "token near/past expiry; Hermes keepalive should rotate it\n"
                )
            _cache.update(token=tok, ts=now, exp=exp_ts)
            return tok
        except Exception:
            if _cache["token"]:
                return _cache["token"]  # serve stale rather than fail
            raise


def receipt_db():
    home = Path(os.environ.get("HERMES_HOME", "/home/ubuntu/.hermes"))
    path = home / "cache" / "honcho-receipts" / "routes.sqlite3"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    c = sqlite3.connect(path, timeout=5)
    c.execute("PRAGMA synchronous=FULL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS routes (id TEXT PRIMARY KEY, started REAL, finished REAL, caller TEXT, operation TEXT, model TEXT, endpoint TEXT, status INTEGER, error_class TEXT)"
    )
    return c


def route_begin(caller, path, model):
    ident = uuid.uuid4().hex
    operation = {"/v1/embeddings": "embedding", "/v1/chat/completions": "chat"}.get(
        urlsplit(path).path, "other"
    )
    # Model names only. Never serialize payloads, headers, URLs with credentials,
    # exception messages, prompts, response bodies or arbitrary query parameters.
    model = (
        model
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", model)
        else "unknown"
    )
    endpoint = urlsplit(UPSTREAM)
    with receipt_db() as c:
        c.execute(
            "INSERT INTO routes VALUES(?,?,NULL,?,?,?,?,NULL,NULL)",
            (
                ident,
                time.time(),
                caller,
                operation,
                model,
                endpoint.scheme + "://" + (endpoint.hostname or ""),
            ),
        )
        c.execute(
            "DELETE FROM routes WHERE finished IS NOT NULL AND finished < ?",
            (time.time() - 7 * 86400,),
        )
    c.close()
    return ident


def route_finish(ident, status, error_class=None):
    with receipt_db() as c:
        c.execute(
            "UPDATE routes SET finished=?,status=?,error_class=? WHERE id=?",
            (time.time(), status, error_class, ident),
        )
    c.close()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet; journald noise not wanted per watchdog conventions

    def _reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "route_receipt", None):
            self.send_header("X-Honcho-Route-Receipt", self.route_receipt)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/"):
            try:
                live_token()
                self._reply(200, {"status": "ok"})
            except Exception as e:
                self._reply(503, {"status": "no-token", "error": str(e)[:120]})
            return
        # GET passthrough (e.g. /v1/models) for smoke checks
        if self.path.startswith("/v1/"):
            try:
                tok = live_token()
                req = urllib.request.Request(
                    UPSTREAM + self.path, headers={"Authorization": "Bearer " + tok}
                )
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        self._reply(r.status, r.read())
                except urllib.error.HTTPError as e:
                    self._reply(e.code, e.read())
            except Exception as e:
                self._reply(502, {"error": str(e)[:200]})
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/"):
            return self._reply(404, {"error": "not found"})
        receipt = None
        status = None
        failure = None
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            model = None
            try:
                payload = json.loads(body)
                model = payload.get("model")
                if isinstance(model, str) and model.endswith("qwen3-embedding-8b"):
                    payload["dimensions"] = 1536
                body = json.dumps(payload).encode()
            except (ValueError, AttributeError, TypeError):
                pass
            try:
                receipt = route_begin(self.client_address[0], self.path, model)
                self.route_receipt = receipt
            except Exception:
                sys.stderr.write("route receipt admission unavailable\n")
            tok = live_token()
            req = urllib.request.Request(
                UPSTREAM + self.path,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + tok,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    status = r.status
                    self._reply(r.status, r.read())
            except urllib.error.HTTPError as e:
                status = e.code
                failure = "HTTPError"
                self._reply(e.code, e.read())
        except Exception as e:
            failure = type(e).__name__
            try:
                self._reply(
                    502, {"error": "upstream request failed", "error_class": failure}
                )
            except (BrokenPipeError, ConnectionError):
                pass
        finally:
            if receipt:
                try:
                    route_finish(receipt, status, failure)
                except Exception:
                    sys.stderr.write("route receipt completion unavailable\n")


if __name__ == "__main__":
    bind = os.environ.get("NOUS_PROXY_BIND", "0.0.0.0")
    srv = ThreadingHTTPServer((bind, PORT), H)
    sys.stderr.write(f"nous-auth-proxy listening on {bind}:{PORT}\n")
    srv.serve_forever()
