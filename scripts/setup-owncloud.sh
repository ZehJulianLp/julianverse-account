#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID == 0 ]] || { echo 'Bitte mit sudo ausführen.' >&2; exit 1; }
exec "$repository/.venv/bin/python" "$repository/scripts/setup-owncloud.py"
