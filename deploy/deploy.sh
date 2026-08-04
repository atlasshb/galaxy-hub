#!/usr/bin/env bash
# deploy.sh — Galaxy Hub env deploy (atlas-01)
# Usage: ./deploy.sh <dev|staging|prod>
#
# Branch -> env mapping:
#   dev     -> branch dev     -> /opt/atlas/galaxyhub-dev     -> port 8880
#   staging -> branch staging -> /opt/atlas/galaxyhub-staging -> port 8879
#   prod    -> branch main    -> /opt/atlas/galaxyhub         -> port 8877
set -euo pipefail

ENV="${1:?usage: deploy.sh <dev|staging|prod>}"
REPO="https://github.com/atlasshb/galaxy-hub.git"
TOKEN_ENV="${GH_TOKEN_FILE:-/opt/atlas/galaxyhub/.token}"

case "$ENV" in
  dev)     BRANCH="dev";     DIR="/opt/atlas/galaxyhub-dev";     PORT=8880; UNIT="atlas-galaxyhub-dev" ;;
  staging) BRANCH="staging"; DIR="/opt/atlas/galaxyhub-staging"; PORT=8879; UNIT="atlas-galaxyhub-staging" ;;
  prod)    BRANCH="main";    DIR="/opt/atlas/galaxyhub";         PORT=8877; UNIT="atlas-galaxyhub" ;;
  *) echo "unknown env '$ENV' (dev|staging|prod)" >&2; exit 1 ;;
esac

if [ ! -d "$DIR/.git" ]; then
  echo ">> cloning $REPO branch $BRANCH -> $DIR"
  git clone -b "$BRANCH" "$REPO" "$DIR"
fi

cd "$DIR"
echo ">> fetching origin"
git fetch --quiet origin
git checkout "$BRANCH" 2>/dev/null || git checkout -B "$BRANCH" origin/"$BRANCH"
git reset --hard --quiet origin/"$BRANCH"

# Fail fast on syntax errors before touching systemd.
echo ">> py_compile stardrive.py"
python3 -m py_compile stardrive.py

# Ensure a token file exists (gitignored, kept local per env).
TOKEN_FILE="$DIR/.token"
if [ ! -s "$TOKEN_FILE" ]; then
  umask 177
  openssl rand -hex 24 > "$TOKEN_FILE"
  echo ">> generated fresh token file $TOKEN_FILE"
fi

echo ">> restarting $UNIT"
systemctl restart "$UNIT"
systemctl --no-pager --lines=5 status "$UNIT" | tail -6
echo ">> done: $ENV @ http://127.0.0.1:$PORT"
