#!/usr/bin/env bash
# Delegate to the shared launcher.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$project_dir/scripts/start_lighter_arcus.sh" "$@"
