from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy" / "verify_image_attestation.py"
DIGEST = "sha256:" + "a" * 64
SOURCE_SHA = "b" * 40
REPOSITORY = "ghcr.io/batumilove/hermes-agent-deploy"
SOURCE_URI = "git+https://github.com/batumilove/hermes-agent@refs/heads/batumi/live"


def _module() -> ModuleType:
    assert SCRIPT.exists(), "image attestation verifier script is missing"
    spec = importlib.util.spec_from_file_location("verify_image_attestation", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(
    *,
    repository: str = REPOSITORY,
    digest: str = DIGEST,
    source_sha: str = SOURCE_SHA,
    source_uri: str = SOURCE_URI,
) -> dict:
    return {
        "verificationResult": {
            "statement": {
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [
                    {
                        "name": repository,
                        "digest": {"sha256": digest.removeprefix("sha256:")},
                    }
                ],
                "predicate": {
                    "buildDefinition": {
                        "resolvedDependencies": [
                            {
                                "digest": {"gitCommit": source_sha},
                                "uri": source_uri,
                            }
                        ]
                    }
                },
            }
        }
    }


def test_exact_subject_source_commit_and_uri_are_accepted() -> None:
    module = _module()

    module.verify_attestation_payload(
        [_entry()], REPOSITORY, DIGEST, SOURCE_SHA, SOURCE_URI
    )


def test_wrong_source_commit_is_rejected() -> None:
    module = _module()

    with pytest.raises(ValueError, match="does not bind"):
        module.verify_attestation_payload(
            [_entry(source_sha="c" * 40)],
            REPOSITORY,
            DIGEST,
            SOURCE_SHA,
            SOURCE_URI,
        )


def test_subject_and_source_split_across_statements_are_rejected() -> None:
    module = _module()
    subject_only = _entry()
    subject_only["verificationResult"]["statement"]["predicate"] = {}
    source_only = _entry()
    source_only["verificationResult"]["statement"]["subject"] = []

    with pytest.raises(ValueError, match="does not bind"):
        module.verify_attestation_payload(
            [subject_only, source_only],
            REPOSITORY,
            DIGEST,
            SOURCE_SHA,
            SOURCE_URI,
        )


def test_wrong_source_uri_and_predicate_type_are_rejected() -> None:
    module = _module()

    with pytest.raises(ValueError, match="does not bind"):
        module.verify_attestation_payload(
            [_entry(source_uri="git+https://github.com/other/repo@refs/heads/main")],
            REPOSITORY,
            DIGEST,
            SOURCE_SHA,
            SOURCE_URI,
        )

    wrong_predicate = _entry()
    wrong_predicate["verificationResult"]["statement"]["predicateType"] = "custom"
    with pytest.raises(ValueError, match="does not bind"):
        module.verify_attestation_payload(
            [wrong_predicate], REPOSITORY, DIGEST, SOURCE_SHA, SOURCE_URI
        )


def test_wrong_subject_repository_and_digest_are_rejected() -> None:
    module = _module()

    with pytest.raises(ValueError, match="does not bind"):
        module.verify_attestation_payload(
            [_entry(repository="ghcr.io/other/image")],
            REPOSITORY,
            DIGEST,
            SOURCE_SHA,
            SOURCE_URI,
        )
    with pytest.raises(ValueError, match="does not bind"):
        module.verify_attestation_payload(
            [_entry(digest="sha256:" + "c" * 64)],
            REPOSITORY,
            DIGEST,
            SOURCE_SHA,
            SOURCE_URI,
        )


def test_malformed_expected_identifiers_are_rejected() -> None:
    module = _module()

    with pytest.raises(ValueError, match="expected digest"):
        module.verify_attestation_payload(
            [_entry()], REPOSITORY, "latest", SOURCE_SHA, SOURCE_URI
        )
    with pytest.raises(ValueError, match="source SHA"):
        module.verify_attestation_payload(
            [_entry()], REPOSITORY, DIGEST, "main", SOURCE_URI
        )
