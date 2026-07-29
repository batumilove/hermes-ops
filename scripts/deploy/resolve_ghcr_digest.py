#!/usr/bin/env python3
"""Resolve an immutable GHCR tag without treating registry errors as absence."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def _open(opener: Callable, request: urllib.request.Request):
    try:
        return opener(request, timeout=30)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GHCR request failed: {exc.reason}") from exc


def resolve_manifest_digest(
    repository: str,
    reference: str,
    *,
    username: str,
    password: str,
    opener: Callable = urllib.request.urlopen,
) -> str | None:
    """Return a GHCR manifest digest, None only for a definitive HTTP 404."""
    prefix = "ghcr.io/"
    if not repository.startswith(prefix) or repository.count("/") < 2:
        raise ValueError("repository must be a ghcr.io owner/image path")
    if not _TAG.fullmatch(reference):
        raise ValueError("reference must be a valid container tag")
    if not username or not password:
        raise ValueError("GHCR credentials are required")

    image_path = repository.removeprefix(prefix)
    query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": f"repository:{image_path}:pull"}
    )
    basic = base64.b64encode(f"{username}:{password}".encode()).decode()
    token_request = urllib.request.Request(
        f"https://ghcr.io/token?{query}",
        headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
    )
    try:
        with _open(opener, token_request) as response:
            token_payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GHCR token request failed: {exc.code}") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("GHCR token response was not valid JSON") from exc

    registry_token = token_payload.get("token") or token_payload.get("access_token")
    if not isinstance(registry_token, str) or not registry_token:
        raise RuntimeError("GHCR token response did not contain a token")

    manifest_request = urllib.request.Request(
        f"https://ghcr.io/v2/{image_path}/manifests/{reference}",
        method="GET",
        headers={"Authorization": f"Bearer {registry_token}", "Accept": _ACCEPT},
    )
    try:
        with _open(opener, manifest_request) as response:
            digest = response.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            try:
                error_payload = json.loads(exc.read())
            except (json.JSONDecodeError, TypeError) as parse_error:
                raise RuntimeError("non-definitive GHCR 404 response") from parse_error
            if not isinstance(error_payload, dict):
                raise RuntimeError("non-definitive GHCR 404 response") from exc
            errors = error_payload.get("errors", [])
            if (
                isinstance(errors, list)
                and len(errors) == 1
                and isinstance(errors[0], dict)
                and errors[0].get("code") == "MANIFEST_UNKNOWN"
            ):
                return None
            raise RuntimeError("non-definitive GHCR 404 response") from exc
        raise RuntimeError(f"unexpected GHCR response: {exc.code}") from exc

    if not _DIGEST.fullmatch(digest):
        raise RuntimeError("invalid Docker-Content-Digest returned by GHCR")
    return digest


def write_github_output(path: Path, digest: str | None) -> None:
    path.write_text(
        f"exists={'true' if digest else 'false'}\n"
        f"digest={digest or ''}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    digest = resolve_manifest_digest(
        args.repository,
        args.reference,
        username=os.environ.get("GHCR_USERNAME", ""),
        password=os.environ.get("GHCR_TOKEN", ""),
    )
    write_github_output(args.github_output, digest)
    if digest:
        print(f"Reusing existing immutable image digest {digest}")
    else:
        print("No existing source-SHA image tag; build is required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
