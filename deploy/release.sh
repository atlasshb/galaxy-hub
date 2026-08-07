#!/usr/bin/env bash
# release.sh — headless release-swarm wrapper.
# Usage: ./release.sh [staging|prod]   (default: staging)
set -euo pipefail
TARGET="${1:-staging}"
case "$TARGET" in
  staging|prod) ;;
  *) echo "usage: $0 [staging|prod]" >&2; exit 1 ;;
esac

if ! command -v opencode >/dev/null 2>&1; then
  echo "opencode CLI not found on PATH" >&2
  exit 1
fi

echo ">> release swarm: $TARGET"
opencode run --agent release-orchestrator --auto "release $TARGET"
