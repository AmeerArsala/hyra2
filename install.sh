#!/usr/bin/env bash
# Install Hyra for Claude Code: the `hyra` CLI on PATH plus user-level agents and skills.
# Everything is symlinked back to this repo, so edits in this repo take effect immediately. Undo with ./uninstall.sh.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${HYRA_BIN_DIR:-$HOME/.local/bin}"
CLAUDE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

link() {  # link <src> <dest>
  if [ -e "$2" ] && [ ! -L "$2" ]; then
    echo "skip: $2 exists and is not a symlink (remove it to let hyra manage it)"; return
  fi
  ln -sfn "$1" "$2"; echo "linked $2 -> $1"
}

mkdir -p "$BIN" "$CLAUDE/agents" "$CLAUDE/skills" "$HOME/.hyra/library/books" "$HOME/.hyra/library/skills"
link "$HERE/bin/hyra" "$BIN/hyra"
for f in "$HERE"/claude/agents/*.md; do link "$f" "$CLAUDE/agents/$(basename "$f")"; done
for d in "$HERE"/claude/skills/*/; do d="${d%/}"; link "$d" "$CLAUDE/skills/$(basename "$d")"; done

case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to your PATH";; esac
echo "done. In any Claude Code session: /hyra <research goal>   (CLI: hyra --help)"
