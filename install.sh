#!/usr/bin/env bash
# Galaxy Hub — one-command install / update.
# Idempotent: clones on first run, git-pulls after. Then tells you how to launch.
#
#   curl -fsSL https://raw.githubusercontent.com/atlasshb/galaxy-hub/main/install.sh | bash
#
# Personal device (laptop, MET, MET2):   just run it, then `python3 stardrive.py`
# Shared server (atlas01 behind a domain): see DEPLOY-VPS.md — do NOT expose without --token.

set -euo pipefail

REPO_URL="https://github.com/atlasshb/galaxy-hub"
DEST="${GALAXY_HUB_DIR:-$HOME/galaxy-hub}"
PORT="${GALAXY_HUB_PORT:-8877}"

say()  { printf '\033[1;33m▸ %s\033[0m\n' "$*"; }   # gold
note() { printf '  %s\n' "$*"; }

command -v git     >/dev/null || { echo "git is required"; exit 1; }
command -v python3 >/dev/null || { echo "python3 (3.8+) is required"; exit 1; }

if [ -d "$DEST/.git" ]; then
  say "Updating Galaxy Hub in $DEST"
  git -C "$DEST" pull --ff-only
else
  say "Installing Galaxy Hub to $DEST"
  git clone "$REPO_URL" "$DEST"
fi

cd "$DEST"
python3 -m py_compile stardrive.py && say "stardrive.py OK"

cat <<EOF

$(printf '\033[1;33m✓ Galaxy Hub ready in %s\033[0m' "$DEST")

Launch it (personal device — loopback only, safe by default):
    cd "$DEST" && python3 stardrive.py --port $PORT
    open http://127.0.0.1:$PORT

It reads THIS machine's ~/.claude/projects (read-only). Run it on the box whose
Claude Code history you want to see.

Options:
    --enable-run          let the Chat/Orchestration apps actually run the claude CLI
    --token <secret>      require a token (MANDATORY before binding a non-loopback IP)
    --max-runs N          parallel Orchestration lanes (1-8, default 3)

Reaching it from another device:
    Safe way A (no exposure): SSH tunnel  →  see DEPLOY.md
    Safe way B (tailnet/LAN):  --bind <tailnet-ip> --token <secret>  →  see DEPLOY.md
    Public domain (atlas01):   Caddy TLS + --token  →  see DEPLOY-VPS.md

EOF
