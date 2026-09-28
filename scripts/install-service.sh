#!/usr/bin/env bash
# Run as the repository owner, without sudo. The existing user manager has linger enabled.
set -Eeuo pipefail
umask 077
repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID != 0 ]] || { echo 'Bitte als normaler Benutzer ohne sudo ausführen.' >&2; exit 1; }
[[ -f "$repository/.env" && -x "$repository/.venv/bin/gunicorn" ]] || {
    echo 'Bitte zuerst Installation und init-secrets laut README durchführen.' >&2; exit 1;
}
cd -- "$repository"
"$repository/.venv/bin/python" - <<'PY'
from wsgi import app
if app.config['ENVIRONMENT'] != 'production' or app.config['BASE_URL'] != 'https://account.julianverse.de':
    raise SystemExit('Bitte .env zuerst auf production und https://account.julianverse.de einstellen.')
PY
"$repository/.venv/bin/flask" --app wsgi db upgrade
install -d -m 0700 "$HOME/.config/systemd/user"
"$repository/.venv/bin/python" - <<'PY'
from pathlib import Path
root = Path.cwd()
if any(c in str(root) for c in (' ', '\n', '\r', '"', '%', '\\')):
    raise SystemExit('Bitte einen Installationspfad ohne Leerzeichen/Sonderzeichen verwenden.')
target = Path.home() / '.config/systemd/user/julianverse-account.service'
text = (root / 'deploy/julianverse-account.service').read_text().replace('@ROOT@', str(root))
if target.exists():
    previous = target.read_text()
    if 'Description=Julianverse Account (Flask / Gunicorn)' not in previous:
        raise SystemExit('Der Dienstname wird bereits von einer anderen Konfiguration verwendet.')
    target.with_suffix('.service.bak').write_text(previous)
target.write_text(text)
PY
systemctl --user daemon-reload
systemctl --user enable julianverse-account.service
systemctl --user restart julianverse-account.service
for attempt in {1..30}; do
    if curl --fail --silent --max-time 2 -H 'Host: account.julianverse.de' http://127.0.0.1:8096/healthz; then
        echo
        echo 'Account läuft auf 127.0.0.1:8096.'
        echo 'Beim ersten Start: sudo bash scripts/setup-nginx.sh'
        exit 0
    fi
    sleep 1
done
echo 'Der Dienst ist nicht bereit. Prüfen: journalctl --user -u julianverse-account -n 30' >&2
exit 1
