#!/usr/bin/env python3
"""Evaluate the exact-source GitHub check required for deployment."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


GITHUB_ACTIONS_APP_ID = 15368


def evaluate_gate(
    *,
    expected_sha: str,
    actual_sha: str,
    required_context: str,
    payload: dict[str, Any],
    app_id: int = GITHUB_ACTIONS_APP_ID,
) -> str:
    if actual_sha != expected_sha:
        raise ValueError(
            "Refusing stale deployment: "
            f"branch head={actual_sha} workflow={expected_sha}"
        )

    check_runs = payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise ValueError("invalid check-runs payload")

    matching_runs = [
        run
        for run in check_runs
        if isinstance(run, dict)
        and run.get("name") == required_context
        and isinstance(run.get("app"), dict)
        and run["app"].get("id") == app_id
    ]
    matching_runs.sort(key=lambda run: run.get("started_at") or "")
    run = matching_runs[-1] if matching_runs else {}
    return f"{run.get('status', 'missing')}:{run.get('conclusion') or 'pending'}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--actual-sha", required=True)
    parser.add_argument("--required-context", required=True)
    args = parser.parse_args()

    try:
        result = evaluate_gate(
            expected_sha=args.expected_sha,
            actual_sha=args.actual_sha,
            required_context=args.required_context,
            payload=json.load(sys.stdin),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())