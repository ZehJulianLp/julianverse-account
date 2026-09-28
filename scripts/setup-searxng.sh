#!/usr/bin/env bash
set -Eeuo pipefail
repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$repository/scripts/setup-searxng.py" "$@"
