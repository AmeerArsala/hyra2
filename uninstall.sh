#!/usr/bin/env bash
# Remove the symlinks created by install.sh. Leaves ~/.hyra/library and project .hyra/ dirs alone.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/hyra/scripts/hyra" setup --uninstall "$@"
