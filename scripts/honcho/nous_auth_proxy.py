#!/usr/bin/env python3
"""Credentialed inference proxy with private, content-free request receipts.

Remote callers must be explicitly allowlisted. Token refresh remains owned by
Hermes keepalive; this process only reads the existing shared auth store.
"""

import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

AUTH_PATH = os.environ.get(
    "NOUS_AUTH_PATH", "/home/ubuntu/.hermes/shared/nous_auth.json"
)
UPSTREAM = "https://inference-api.nousresearch.com"
PORT = int(os.environ.get("NOUS_PROXY_PORT", "18089"))
BODY_TIMEOUT = 20.0
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
RESPONSE_TIMEOUT = 120.0
TTL = 30.0
_lock = threading.Lock()
_cache = {"token": None, "ts": 0.0, "exp": 0.0}


def allowed_caller(address):
    try:
        ip = ipaddress.ip_address(address)
        networks = os.environ.get("NOUS_PROXY_ALLOWLIST", "127.0.0.1/32,::1/128").split(
            ","
        )
        return any(
            ip in ipaddress.ip_network(n.strip(), strict=False)
            for n in networks
            if n.strip()
        )
    except ValueError:
        return False


def origin(url):
    u = urlsplit(url)
    return (u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80))


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(newurl) != origin(UPSTREAM):
            raise urllib.error.URLError("cross_origin_redirect_blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_upstream(req, timeout):
    # Default urllib redirects can carry the Authorization header to a different
    # origin. Both GET and POST use the same trusted-origin policy.
    return urllib.request.build_opener(TrustedRedirect()).open(req, timeout=timeout)


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


PURPOSE_STAGES = {
    "deriver.representation": "deriver",
    "dialectic.answer": "dialectic",
    "dream.deduction": "dream",
    "dream.induction": "dream",
    "summary.short": "summary",
    "summary.long": "summary",
}


def correlation_fields(headers, operation):
    ident = headers.get("X-Honcho-Request-ID")
    stage = headers.get("X-Honcho-Stage")
    purpose = headers.get("X-Honcho-Call-Purpose")
    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
        return None, "unattributed", None
    if stage == "embedding" and not purpose and operation == "embedding":
        return ident, stage, None
    if (
        operation == "chat"
        and isinstance(purpose, str)
        and PURPOSE_STAGES.get(purpose) == stage
    ):
        return ident, stage, purpose
    return ident, "unattributed", None


@contextmanager
def receipt_db():
    home = Path(os.environ.get("HERMES_HOME", "/home/ubuntu/.hermes"))
    path = Path(
        os.environ.get(
            "NOUS_PROXY_RECEIPT_PATH",
            str(home / "cache/honcho-receipts/routes.sqlite3"),
        )
    )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    c = sqlite3.connect(path, timeout=5)
    try:
        c.execute("PRAGMA synchronous=FULL")
        c.execute(
            "CREATE TABLE IF NOT EXISTS routes (id TEXT PRIMARY KEY, started REAL, finished REAL, caller TEXT, operation TEXT, model TEXT, endpoint TEXT, status INTEGER, error_class TEXT)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS routes_finished ON routes(finished)")
        c.execute("CREATE INDEX IF NOT EXISTS routes_started ON routes(started)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS correlations (route_id TEXT PRIMARY KEY, request_id TEXT, stage TEXT NOT NULL, purpose TEXT)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS correlation_request ON correlations(request_id)"
        )
        with c:
            yield c
    finally:
        c.close()


def route_begin(caller, path, model, headers=None):
    ident = uuid.uuid4().hex
    operation = {"/v1/embeddings": "embedding", "/v1/chat/completions": "chat"}.get(
        urlsplit(path).path, "other"
    )
    model = (
        model
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", model)
        else "unknown"
    )
    u = urlsplit(UPSTREAM)
    with receipt_db() as c:
        c.execute(
            "INSERT INTO routes VALUES(?,?,NULL,?,?,?,?,NULL,NULL)",
            (
                ident,
                time.time(),
                caller,
                operation,
                model,
                u.scheme + "://" + (u.hostname or ""),
            ),
        )
        request_id, stage, purpose = correlation_fields(headers or {}, operation)
        c.execute(
            "INSERT INTO correlations VALUES(?,?,?,?)",
            (ident, request_id, stage, purpose),
        )
        old = c.execute(
            "SELECT id FROM routes WHERE finished < ? ORDER BY finished LIMIT 500",
            (time.time() - 7 * 86400,),
        ).fetchall()
        c.executemany("DELETE FROM correlations WHERE route_id=?", old)
        c.executemany("DELETE FROM routes WHERE id=?", old)
    return ident


def route_finish(ident, status, error_class=None, actual_url=None):
    with receipt_db() as c:
        if actual_url:
            u = urlsplit(actual_url)
            endpoint = (
                u.scheme
                + "://"
                + (u.hostname or "")
                + (":" + str(u.port) if u.port else "")
            )
            c.execute("UPDATE routes SET endpoint=? WHERE id=?", (endpoint, ident))
        c.execute(
            "UPDATE routes SET finished=?,status=?,error_class=? WHERE id=?",
            (time.time(), status, error_class, ident),
        )


class ProxyServer(ThreadingHTTPServer):
    def verify_request(self, request, client_address):
        # Reject unauthorized sockets before allocating a request thread or
        # reading headers, loading a token, or using provider quota.
        return allowed_caller(client_address[0])


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(BODY_TIMEOUT)
        super().setup()

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._response_started = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "route_receipt", None):
            self.send_header("X-Honcho-Route-Receipt", self.route_receipt)
        self.end_headers()
        self.wfile.write(data)

    def _authorize(self):
        if allowed_caller(self.client_address[0]):
            return True
        self.close_connection = True
        self._reply(403, {"error": "caller not allowed"})
        return False

    def _body(self):
        raw = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not raw.isdecimal():
            raise ValueError("invalid_length")
        size = int(raw)
        if size > MAX_REQUEST_BYTES:
            raise OverflowError("request_too_large")
        deadline = time.monotonic() + BODY_TIMEOUT
        body = bytearray()
        while len(body) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("body_deadline")
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(min(65536, size - len(body)))
            if not chunk:
                raise ValueError("incomplete_body")
            body.extend(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("body_deadline")
        return bytes(body)

    def _response_chunks(self, response):
        total = 0
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("response_deadline")
            chunk = response.read1(65536)
            if time.monotonic() > deadline:
                raise TimeoutError("response_deadline")
            if not chunk:
                return
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise OverflowError("response_too_large")
            yield chunk

    def _forward(self, response, code, stream=False):
        ctype = response.headers.get("Content-Type", "application/json")
        if not stream:
            data = bytearray()
            for chunk in self._response_chunks(response):
                data.extend(chunk)
            self._reply(code, bytes(data), ctype)
            return
        self._response_started = True
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Connection", "close")
        if self.route_receipt:
            self.send_header("X-Honcho-Route-Receipt", self.route_receipt)
        self.end_headers()
        for chunk in self._response_chunks(response):
            self.wfile.write(chunk)
            self.wfile.flush()

    def do_GET(self):
        self.route_receipt = None
        self._response_started = False
        if not self._authorize():
            return
        if self.path in ("/health", "/"):
            try:
                live_token()
                self._reply(200, {"status": "ok"})
            except Exception:
                self._reply(503, {"status": "no-token"})
            return
        if self.path.startswith("/v1/"):
            try:
                req = urllib.request.Request(
                    UPSTREAM + self.path,
                    headers={"Authorization": "Bearer " + live_token()},
                )
                try:
                    with open_upstream(req, 60) as response:
                        self._forward(response, response.status)
                except urllib.error.HTTPError as exc:
                    self._forward(exc, exc.code)
            except Exception as exc:
                if not self._response_started:
                    self._reply(
                        502,
                        {
                            "error": "upstream request failed",
                            "error_class": type(exc).__name__,
                        },
                    )
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        # A persistent HTTP connection reuses this handler for multiple requests.
        self.route_receipt = None
        self._response_started = False
        if not self._authorize():
            return
        if not self.path.startswith("/v1/"):
            self.close_connection = True
            return self._reply(404, {"error": "not found"})
        try:
            body = self._body()
        except (ValueError, OverflowError, TimeoutError, socket.timeout) as exc:
            self.close_connection = True
            code = (
                413
                if isinstance(exc, OverflowError)
                else 408
                if isinstance(exc, TimeoutError)
                else 400
            )
            return self._reply(code, {"error": "invalid request body"})
        receipt, status, failure, actual_url = None, None, None, None
        stream = False
        try:
            model = None
            try:
                payload = json.loads(body)
                model = payload.get("model")
                stream = payload.get("stream") is True
                if isinstance(model, str) and model.endswith("qwen3-embedding-8b"):
                    payload["dimensions"] = 1536
                body = json.dumps(payload).encode()
            except (ValueError, AttributeError, TypeError):
                pass
            try:
                receipt = route_begin(
                    self.client_address[0], self.path, model, headers=self.headers
                )
                self.route_receipt = receipt
            except Exception:
                sys.stderr.write("route receipt admission unavailable\n")
            req = urllib.request.Request(
                UPSTREAM + self.path,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + live_token(),
                },
            )
            try:
                with open_upstream(req, 120) as response:
                    status = response.status
                    actual_url = response.geturl()
                    self._forward(response, status, stream=stream)
            except urllib.error.HTTPError as exc:
                status = exc.code
                actual_url = exc.geturl()
                failure = "HTTPError"
                self._forward(exc, status)
        except Exception as exc:
            failure = type(exc).__name__
            if not self._response_started:
                try:
                    self._reply(
                        502,
                        {"error": "upstream request failed", "error_class": failure},
                    )
                except (BrokenPipeError, ConnectionError):
                    pass
            else:
                self.close_connection = True
        finally:
            if receipt:
                try:
                    route_finish(receipt, status, failure, actual_url)
                except Exception:
                    sys.stderr.write("route receipt completion unavailable\n")


if __name__ == "__main__":
    bind = os.environ.get("NOUS_PROXY_BIND", "127.0.0.1")
    server = ProxyServer((bind, PORT), H)
    sys.stderr.write(f"nous-auth-proxy listening on {bind}:{PORT}\n")
    server.serve_forever()
