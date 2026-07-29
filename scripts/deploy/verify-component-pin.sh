#!/usr/bin/env bash
set -euo pipefail

repo=${1:?repository path required}
expected=${2:?expected commit SHA required}

[[ $expected =~ ^[0-9a-f]{40}$ ]] || {
  printf 'invalid component pin: %s\n' "$expected" >&2
  exit 2
}

actual=$(git -C "$repo" rev-parse HEAD)
test "$actual" = "$expected" || {
  printf 'component pin mismatch: expected=%s actual=%s\n' "$expected" "$actual" >&2
  exit 1
}
git -C "$repo" diff --quiet
git -C "$repo" diff --cached --quiet
