#!/usr/bin/env python3
"""Select the protected source ref and CI gate for an image build."""

from __future__ import annotations

import argparse
from pathlib import Path


POLICIES = {
    "live": ("batumi/live", "shadow / shadow"),
    "candidate": ("rebuild/batumi-live", "shadow / shadow"),
}


def select_policy(lane: str) -> tuple[str, str]:
    try:
        return POLICIES[lane]
    except KeyError as exc:
        raise ValueError(f"unknown deployment lane: {lane}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", required=True, choices=sorted(POLICIES))
    parser.add_argument("--github-env", required=True, type=Path)
    args = parser.parse_args()

    source_branch, required_context = select_policy(args.lane)
    with args.github_env.open("a", encoding="utf-8") as env_file:
        env_file.write(f"source_branch={source_branch}\n")
        env_file.write(f"required_context={required_context}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
