#!/usr/bin/env bash
# Install the skills toolkit for every agent harness on this machine (symlinks, edits go live). Also installs the CLI if missing.
# Options: --uninstall, and for toolkits: --skills a,b  --mode copy|link  --no-cli  (local: --project DIR  --all-harnesses)
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/hyra/scripts/hyra" setup --toolkit global "$@"
