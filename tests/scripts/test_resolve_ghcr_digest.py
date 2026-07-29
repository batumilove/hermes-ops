from __future__ import annotations

import importlib.util
import json
import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import ModuleType

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy" / "resolve_ghcr_digest.py"
DIGEST = "sha256:" + "a" * 64


def _http_error(
    url: str, status: int, reason: str, body: bytes = b""
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, reason, Message(), BytesIO(body))


class _Response:
    def __init__(self, *, body: bytes = b"", headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _module() -> ModuleType:
    assert SCRIPT.exists(), "GHCR resolver script is missing"
    spec = importlib.util.spec_from_file_location("resolve_ghcr_digest", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_manifest_returns_registry_digest() -> None:
    module = _module()
    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        if request.full_url.startswith("https://ghcr.io/token?"):
            return _Response(body=json.dumps({"token": "registry-token"}).encode())
        return _Response(headers={"Docker-Content-Digest": DIGEST})

    result = module.resolve_manifest_digest(
        "ghcr.io/batumilove/hermes-agent-deploy",
        "sha-deadbeef",
        username="ci-user",
        password="ci-token",
        opener=opener,
    )

    assert result == DIGEST
    manifest_request, timeout = requests[-1]
    assert manifest_request.method == "GET"
    assert manifest_request.headers["Authorization"] == "Bearer registry-token"
    assert timeout == 30


def test_manifest_unknown_is_the_only_absent_result() -> None:
    module = _module()

    def opener(request, *, timeout):
        if request.full_url.startswith("https://ghcr.io/token?"):
            return _Response(body=b'{"token":"registry-token"}')
        raise _http_error(
            request.full_url,
            404,
            "not found",
            b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}',
        )

    assert (
        module.resolve_manifest_digest(
            "ghcr.io/batumilove/hermes-agent-deploy",
            "sha-deadbeef",
            username="ci-user",
            password="ci-token",
            opener=opener,
        )
        is None
    )


def test_non_registry_404_fails_closed() -> None:
    module = _module()

    def opener(request, *, timeout):
        if request.full_url.startswith("https://ghcr.io/token?"):
            return _Response(body=b'{"token":"registry-token"}')
        raise _http_error(request.full_url, 404, "not found", b"proxy not found")

    with pytest.raises(RuntimeError, match="non-definitive GHCR 404"):
        module.resolve_manifest_digest(
            "ghcr.io/batumilove/hermes-agent-deploy",
            "sha-deadbeef",
            username="ci-user",
            password="ci-token",
            opener=opener,
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"errors":[{"code":"MANIFEST_UNKNOWN"},{"code":"UNAUTHORIZED"}]}',
        b'{"errors":[]}',
        b'{"errors":["MANIFEST_UNKNOWN"]}',
        b"[]",
    ],
)
def test_ambiguous_registry_404_fails_closed(body: bytes) -> None:
    module = _module()

    def opener(request, *, timeout):
        if request.full_url.startswith("https://ghcr.io/token?"):
            return _Response(body=b'{"token":"registry-token"}')
        raise _http_error(request.full_url, 404, "not found", body)

    with pytest.raises(RuntimeError, match="non-definitive GHCR 404"):
        module.resolve_manifest_digest(
            "ghcr.io/batumilove/hermes-agent-deploy",
            "sha-deadbeef",
            username="ci-user",
            password="ci-token",
            opener=opener,
        )


def test_registry_errors_fail_closed_instead_of_triggering_a_rebuild() -> None:
    module = _module()

    def opener(request, *, timeout):
        if request.full_url.startswith("https://ghcr.io/token?"):
            return _Response(body=b'{"token":"registry-token"}')
        raise _http_error(request.full_url, 503, "unavailable")

    with pytest.raises(RuntimeError, match="unexpected GHCR response: 503"):
        module.resolve_manifest_digest(
            "ghcr.io/batumilove/hermes-agent-deploy",
            "sha-deadbeef",
            username="ci-user",
            password="ci-token",
            opener=opener,
        )


def test_malformed_registry_digest_fails_closed() -> None:
    module = _module()

    def opener(request, *, timeout):
        if request.full_url.startswith("https://ghcr.io/token?"):
            return _Response(body=b'{"token":"registry-token"}')
        return _Response(headers={"Docker-Content-Digest": "sha256:not-a-digest"})

    with pytest.raises(RuntimeError, match="invalid Docker-Content-Digest"):
        module.resolve_manifest_digest(
            "ghcr.io/batumilove/hermes-agent-deploy",
            "sha-deadbeef",
            username="ci-user",
            password="ci-token",
            opener=opener,
        )


def test_token_rate_limit_and_missing_token_fail_closed() -> None:
    module = _module()

    def rate_limited(request, *, timeout):
        raise _http_error(request.full_url, 429, "rate limited")

    with pytest.raises(RuntimeError, match="token request failed: 429"):
        module.resolve_manifest_digest(
            "ghcr.io/batumilove/hermes-agent-deploy",
            "sha-deadbeef",
            username="ci-user",
            password="ci-token",
            opener=rate_limited,
        )

    def missing_token(request, *, timeout):
        return _Response(body=b"{}")

    with pytest.raises(RuntimeError, match="did not contain a token"):
        module.resolve_manifest_digest(
            "ghcr.io/batumilove/hermes-agent-deploy",
            "sha-deadbeef",
            username="ci-user",
            password="ci-token",
            opener=missing_token,
        )


def test_github_output_records_reuse_or_absence(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "github-output"

    module.write_github_output(output, DIGEST)
    assert output.read_text() == f"exists=true\ndigest={DIGEST}\n"

    module.write_github_output(output, None)
    assert output.read_text() == "exists=false\ndigest=\n"
