#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/read-yaml-key.sh
. "$SCRIPT_DIR/lib/read-yaml-key.sh"

VAULT_PATH=""; SLUG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault) VAULT_PATH="$2"; shift 2 ;;
    --slug) SLUG="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$VAULT_PATH" && -n "$SLUG" ]] || { echo "Usage: $0 --vault <p> --slug <s>" >&2; exit 2; }

DRAFT="$VAULT_PATH/wiki/_drafts/${SLUG}.md"
[[ -f "$DRAFT" ]] || { echo "draft not found: $DRAFT" >&2; exit 1; }

TYPE=$(lib_read_yaml_key "$DRAFT" "type")
[[ -n "$TYPE" ]] || { echo "draft missing type field" >&2; exit 1; }

# Bash 3.2 portable: case instead of associative array
type_to_dir() {
  case "$1" in
    concept)       echo "concepts" ;;
    decision)      echo "decisions" ;;
    comparison)    echo "comparisons" ;;
    synthesis)     echo "synthesis" ;;
    open-question) echo "open-questions" ;;
    architecture)  echo "architecture" ;;
    org)           echo "orgs" ;;
    entity)        echo "entities" ;;
    *) echo "" ;;
  esac
}

TARGET_DIR=$(type_to_dir "$TYPE")
[[ -n "$TARGET_DIR" ]] || { echo "unknown type: $TYPE" >&2; exit 1; }

mkdir -p "$VAULT_PATH/wiki/$TARGET_DIR"
DEST="$VAULT_PATH/wiki/$TARGET_DIR/${SLUG}.md"

[[ ! -f "$DEST" ]] || { echo "destination already exists: $DEST" >&2; exit 1; }

# Set status: active (replace any existing status line; add one if the draft has none)
TMP=$(mktemp)
awk '
  # Only touch the leading frontmatter block; never a body line or a later horizontal rule.
  NR == 1 && /^---$/ { in_fm = 1; print; next }
  in_fm && /^---$/ { if (!found_status) print "status: active"; in_fm = 0; print; next }
  in_fm && /^status:/ { found_status = 1; print "status: active"; next }
  { print }
' "$DRAFT" > "$TMP"

mv "$TMP" "$DEST"
rm -f "$DRAFT"
chmod 600 "$DEST"

echo "Promoted: $SLUG → $TARGET_DIR/"
