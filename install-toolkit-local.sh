#!/usr/bin/env bash
# Install the skills toolkit into ONE project (default: current dir; or --project DIR), copied so the repo is self-contained. Also installs the CLI globally if missing.
# Options: --uninstall, and for toolkits: --skills a,b  --mode copy|link  --no-cli  (local: --project DIR  --all-harnesses)
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/hyra/scripts/hyra" setup --toolkit local "$@"
