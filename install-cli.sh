#!/usr/bin/env bash
# Install just the hyra CLI (symlink on PATH). Skills are separate: see the toolkit scripts.
# Options: --uninstall, and for toolkits: --skills a,b  --mode copy|link  --no-cli  (local: --project DIR  --all-harnesses)
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/hyra/scripts/hyra" setup --cli "$@"
