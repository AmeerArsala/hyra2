#!/usr/bin/env bash
# Usage: new-vault.sh --name <vault_name> --path <vault_path>
# One-step vault scaffold: writes wiki.config.md, runs init-vault.sh, and seeds agent instructions
# (AGENTS.md for Codex/OpenCode/Antigravity/Pi/..., CLAUDE.md importing it for Claude Code).
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
NAME=""; VPATH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    --path) VPATH="${2:?--path needs a value}"; shift 2 ;;
    *) echo "Usage: $0 --name <vault_name> --path <vault_path>" >&2; exit 2 ;;
  esac
done
[[ -n "$NAME" && -n "$VPATH" ]] || { echo "Usage: $0 --name <vault_name> --path <vault_path>" >&2; exit 2; }
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "vault name must be kebab-case (a-z, 0-9, -)" >&2; exit 2; }

mkdir -p "$VPATH"
VPATH="$(cd "$VPATH" && pwd -P)"
if [[ -e "$VPATH/wiki.config.md" ]]; then echo "vault already initialized at $VPATH" >&2; exit 1; fi

CONFIG="$(mktemp)"; trap 'rm -f "$CONFIG"' EXIT
cat > "$CONFIG" <<CFG
---
vault_name: $NAME
vault_path: $VPATH
schema_version: 0.4.0
page_types:
  - concept
  - entity
  - decision
  - comparison
  - source-summary
  - synthesis
  - open-question
raw_dirs:
  - external
  - internal
---
# $NAME wiki config
CFG
"$SCRIPT_DIR/init-vault.sh" "$VPATH" "$CONFIG"

SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
if [[ ! -f "$VPATH/AGENTS.md" ]]; then
  sed "s#{{SKILL_DIR}}#$SKILL_DIR#g" "$SCRIPT_DIR/templates/_AGENTS.md" > "$VPATH/AGENTS.md"
fi
[[ -f "$VPATH/CLAUDE.md" ]] || printf '@AGENTS.md\n' > "$VPATH/CLAUDE.md"
echo "Vault '$NAME' ready at $VPATH (AGENTS.md + CLAUDE.md seeded)."
