#!/usr/bin/env bash
# Remove the symlinks created by install.sh. Leaves ~/.hyra/library (your Books/skills) and project .hyra/ dirs alone.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${HYRA_BIN_DIR:-$HOME/.local/bin}"
CLAUDE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
unlink_if_ours() { if [ -L "$1" ] && [[ "$(readlink "$1")" == "$HERE"* ]]; then rm "$1"; echo "removed $1"; fi; }
unlink_if_ours "$BIN/hyra"
for f in "$HERE"/claude/agents/*.md; do unlink_if_ours "$CLAUDE/agents/$(basename "$f")"; done
for d in "$HERE"/claude/skills/*/; do d="${d%/}"; unlink_if_ours "$CLAUDE/skills/$(basename "$d")"; done
