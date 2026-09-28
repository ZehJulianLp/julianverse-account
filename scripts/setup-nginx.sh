#!/usr/bin/env bash
# Usage: sudo bash scripts/setup-nginx.sh
set -Eeuo pipefail
umask 077

domain=account.julianverse.de
destination=/etc/nginx/conf.d/julianverse-account.conf
repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
hook_dir=/usr/local/libexec/julianverse-account
backup=
installed=0
was_active=0
success=0

fail() { echo "FEHLER: $*" >&2; exit 1; }
[[ $EUID == 0 ]] || fail "Bitte mit sudo bash scripts/setup-nginx.sh ausführen."
for command in nginx certbot systemctl curl getent openssl flock; do
    command -v "$command" >/dev/null || fail "$command ist nicht installiert."
done
exec 9>/run/julianverse-account-setup.lock
flock -n 9 || fail "Die Einrichtung läuft bereits."
[[ -f "$repository/deploy/account.nginx.conf" ]] || fail "Die Nginx-Vorlage fehlt."
nginx -t
systemctl is-active --quiet nginx && was_active=1
[[ $was_active == 1 ]] || fail "Nginx läuft nicht. Bitte behebe das zuerst."
getent ahostsv4 "$domain" >/dev/null || fail "Der A-Record ist noch nicht auflösbar."
if ! curl --fail --silent --max-time 5 \
    -H "Host: $domain" http://127.0.0.1:8096/healthz >/dev/null; then
    echo "Die Account-App läuft noch nicht. HTTPS wird trotzdem eingerichtet."
    echo "Bis zum App-Start zeigt der neue Proxy eine Wartungsmeldung."
fi

if [[ -e "$destination" ]] && ! head -n 1 "$destination" | grep -q 'Managed by Julianverse Account'; then
    fail "$destination enthält eine fremde Konfiguration. Sie wird nicht überschrieben."
fi
# Refuse duplicate domain blocks outside the managed file, without printing other configs.
while IFS= read -r config; do
    [[ "$config" == "$destination" ]] && continue
    if grep -Eq '^[[:space:]]*server_name[[:space:]][^;]*account\.julianverse\.de([[:space:];]|$)' "$config"; then
        fail "Die Domain steht schon in $config. Bitte den vorhandenen Block zuerst prüfen."
    fi
done < <(find /etc/nginx -type f -name '*.conf')

backup=$(mktemp -d /var/backups/julianverse-account-nginx.XXXXXXXX)
cp -a /etc/nginx/nginx.conf "$backup/nginx.conf"
if [[ -e "$destination" ]]; then cp -a "$destination" "$backup/account.conf"; fi

cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [[ $success != 1 && $installed == 1 ]]; then
        echo "Stelle den bisherigen Account-Proxy wieder her." >&2
        if [[ -f "$backup/account.conf" ]]; then cp -a "$backup/account.conf" "$destination";
        else rm -f -- "$destination"; fi
        nginx -t && systemctl reload-or-restart nginx || true
    fi
    if [[ $was_active == 1 ]] && ! systemctl is-active --quiet nginx; then
        echo "Starte Nginx wieder." >&2
        systemctl start nginx || { echo "FEHLER: Nginx konnte nicht gestartet werden; siehe systemctl status nginx." >&2; result=1; }
    fi
    [[ -x "$hook_dir/cert-post" ]] && "$hook_dir/cert-post" || true
    echo "Sicherung: $backup"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

install -d -m 0755 "$hook_dir"
cat > "$hook_dir/cert-pre" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
umask 077
if systemctl is-active --quiet nginx; then
    touch /run/julianverse-account-cert-nginx-stopped
    systemctl stop nginx
fi
HOOK
cat > "$hook_dir/cert-post" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
if [[ -f /run/julianverse-account-cert-nginx-stopped ]]; then
    nginx -t
    systemctl start nginx
    rm -f /run/julianverse-account-cert-nginx-stopped
fi
HOOK
chmod 0755 "$hook_dir/cert-pre" "$hook_dir/cert-post"

echo "Fordere das Zertifikat mit certbot certonly --standalone an."
echo "Nginx wird dafür kurz gestoppt; alle darüber erreichbaren Webseiten sind kurz unterbrochen."
# Interactive Certbot handles account/email/terms if no ACME account exists yet.
# Saved hooks also cover future renewal attempts, including failed validation.
certbot certonly --standalone --preferred-challenges http \
    --cert-name "$domain" -d "$domain" --keep-until-expiring \
    --pre-hook "$hook_dir/cert-pre" --post-hook "$hook_dir/cert-post"
"$hook_dir/cert-post"
openssl x509 -checkend 0 -noout -in "/etc/letsencrypt/live/$domain/fullchain.pem" >/dev/null

echo "Installiere den zusätzlichen Account-Proxy und prüfe die Gesamtkonfiguration."
install -m 0644 "$repository/deploy/account.nginx.conf" "$destination"
installed=1
nginx -t
systemctl reload nginx
status=$(curl --silent --show-error --max-time 15 \
    --retry 10 --retry-delay 1 --retry-max-time 30 --retry-all-errors \
    --resolve "$domain:443:127.0.0.1" "https://$domain/healthz" \
    --dump-header "$backup/https-check.headers" --output "$backup/https-check.body" \
    --write-out '%{http_code}')
if [[ "$status" == 200 ]]; then
    echo "HTTPS und Account-App sind erreichbar."
elif [[ "$status" == 503 ]] && grep -qi '^X-Julianverse-Setup: pending' "$backup/https-check.headers"; then
    echo "HTTPS ist eingerichtet. Die Wartungsmeldung bleibt bis zum Start der Account-App sichtbar."
else
    fail "Die HTTPS-Prüfung meldet unerwartet HTTP $status."
fi
success=1
echo "Fertig: https://$domain"
echo "Erneuerung prüfen (mit kurzer Nginx-Unterbrechung):"
echo "  sudo certbot renew --cert-name $domain --dry-run"
echo "Ein vorhandener Certbot-Timer bleibt unverändert. Prüfen: systemctl list-timers --all | grep certbot"
