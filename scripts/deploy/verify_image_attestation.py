#!/usr/bin/env python3
"""Validate that verified SLSA evidence binds one image digest to one Git SHA."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


def verify_attestation_payload(
    entries: object,
    repository: str,
    digest: str,
    source_sha: str,
    source_uri: str,
) -> None:
    if not _DIGEST.fullmatch(digest):
        raise ValueError("expected digest must be sha256:<64 lowercase hex characters>")
    if not _SOURCE_SHA.fullmatch(source_sha):
        raise ValueError("expected source SHA must be 40 lowercase hex characters")
    if not repository.startswith("ghcr.io/"):
        raise ValueError("expected repository must be a ghcr.io image path")
    if not source_uri.startswith("git+https://github.com/"):
        raise ValueError("expected source URI must be a GitHub git+https URI")
    if not isinstance(entries, list):
        raise ValueError("attestation evidence must be a JSON array")

    expected_hex = digest.removeprefix("sha256:")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        statement = entry.get("verificationResult", {}).get("statement", {})
        if not isinstance(statement, dict):
            continue
        if statement.get("predicateType") != "https://slsa.dev/provenance/v1":
            continue
        subjects = statement.get("subject", [])
        predicate = statement.get("predicate", {})
        if not isinstance(predicate, dict):
            continue
        build_definition = predicate.get("buildDefinition", {})
        if not isinstance(build_definition, dict):
            continue
        dependencies = build_definition.get("resolvedDependencies", [])
        subject_matches = isinstance(subjects, list) and any(
            isinstance(subject, dict)
            and subject.get("name") == repository
            and isinstance(subject.get("digest"), dict)
            and subject["digest"].get("sha256") == expected_hex
            for subject in subjects
        )
        source_matches = isinstance(dependencies, list) and any(
            isinstance(dependency, dict)
            and dependency.get("uri") == source_uri
            and isinstance(dependency.get("digest"), dict)
            and dependency["digest"].get("gitCommit") == source_sha
            for dependency in dependencies
        )
        if subject_matches and source_matches:
            return

    raise ValueError(
        "attestation does not bind the expected image digest to the expected source SHA"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-uri", required=True)
    args = parser.parse_args()

    entries = json.loads(args.evidence.read_text(encoding="utf-8"))
    verify_attestation_payload(
        entries, args.repository, args.digest, args.source_sha, args.source_uri
    )
    print(
        "Verified reusable image provenance: "
        f"source={args.source_sha} digest={args.digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
