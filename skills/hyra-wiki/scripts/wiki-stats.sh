#!/usr/bin/env bash
# Usage: wiki-stats.sh <vault_path>   |   wiki-stats.sh --vault <vault_path>
# Outputs structured vault metrics: totals, per-section counts, recent activity.
# Format: [section]\nkey=value — designed for LLM agent consumption.
set -Eeuo pipefail
[[ ${BASH_VERSINFO[0]:-0} -ge 4 ]] && shopt -s inherit_errexit
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./lib/read-yaml-key.sh
source "$SCRIPT_DIR/lib/read-yaml-key.sh"

[[ "${1:-}" == "--vault" ]] && shift  # flag form, as the docs use it; positional form kept for compatibility
VAULT_PATH="${1:?Usage: wiki-stats.sh [--vault] <vault_path>}"

if [[ ! -d "${VAULT_PATH}/wiki" ]]; then
  echo "Error: wiki/ not found at ${VAULT_PATH}" >&2
  exit 1
fi

OBS_VAULT=$(lib_read_yaml_key "${VAULT_PATH}/wiki.config.md" "vault_name")
# No Obsidian CLI? Use the bundled filesystem shim so this works from plain markdown (any harness).
export WIKI_VAULT_PATH="${VAULT_PATH}"
[[ "${WIKI_USE_OBSIDIAN_CLI:-}" == 1 ]] && command -v obsidian >/dev/null 2>&1 || export PATH="$SCRIPT_DIR/lib/shim:$PATH"  # real CLI only if opted in
if [[ -z "$OBS_VAULT" ]]; then
  echo "Error: vault_name not found in wiki.config.md" >&2
  exit 1
fi

# --- Totals ---
echo "[totals]"
echo "wiki=$(obsidian vault="$OBS_VAULT" files folder=wiki total < /dev/null 2>/dev/null)"
echo "raw=$(obsidian vault="$OBS_VAULT" files folder=raw total < /dev/null 2>/dev/null)"
echo "tags=$(obsidian vault="$OBS_VAULT" tags total < /dev/null 2>/dev/null)"

# --- Per-section counts (excluding _hub.md) ---
echo ""
echo "[sections]"
for s in concepts entities architecture decisions strategy orgs comparisons open-questions sources synthesis; do
  count=$(obsidian vault="$OBS_VAULT" files folder="wiki/$s" < /dev/null 2>/dev/null | grep -cv '/_hub\.md$' || true)  # grep -c prints 0 itself; '|| echo 0' printed it twice
  echo "$s=$count"
done

# --- Recent activity from log.md ---
echo ""
echo "[activity]"
LOG="${VAULT_PATH}/log.md"
if [[ -f "$LOG" ]]; then
  last_ingest=$(grep '^## \[' "$LOG" | grep 'ingest' | tail -1 | sed 's/^## \[\([0-9-]*\)\].*/\1/' || echo "none")
  last_lint=$(grep '^## \[' "$LOG" | grep 'lint' | tail -1 | sed 's/^## \[\([0-9-]*\)\].*/\1/' || echo "none")
  echo "last_ingest=${last_ingest:-none}"
  echo "last_lint=${last_lint:-none}"
else
  echo "last_ingest=none"
  echo "last_lint=none"
fi
