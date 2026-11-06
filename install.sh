#!/usr/bin/env bash
# Install Hyra for every agent harness on this machine (Claude Code, Codex, Gemini CLI, OpenCode,
# Cursor, Amp, Qwen, Copilot, Goose, Droid — whichever exist) plus the `hyra` CLI on PATH.
# Everything is a symlink back to this repo, so edits take effect immediately. Undo: ./uninstall.sh
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/hyra/scripts/hyra" setup "$@"
