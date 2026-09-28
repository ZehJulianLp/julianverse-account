# Julianverse Account

Eigene Kontoverwaltung für **account.julianverse.de**, gebaut mit Flask, Jinja,
HTML/CSS/JavaScript, SQLite, SQLAlchemy, Alembic und Authlib.

**ownCloud ist die Quelle für synchronisierte App-Inhalte.** SQLite speichert
Konten, verschlüsselte Verbindungsdaten, Sitzungen und Freigaben. Apps bleiben
ohne Anmeldung lokal nutzbar; eine Anmeldung aktiviert keinen Sync.

## Implementiert

- Registrierung, Passwort-Anmeldung, E-Mail-Bestätigung und Passwort-Reset.
- Profil, bestätigter E-Mail-Wechsel, helle/dunkle Darstellung.
- Passkeys mit Gerätebestätigung, TOTP und einmalige Wiederherstellungscodes.
- Aktive Sitzungen anzeigen und widerrufen; zugehörige App-Tokens werden ungültig.
- Discord OAuth2: anmelden, ausdrücklich verknüpfen und trennen. Gleiche E-Mails
  führen niemals zu einer automatischen Zusammenführung.
- OIDC Authorization Code mit S256-PKCE, Zustimmung, Discovery, JWKS, signierten
  ID-Tokens, Userinfo, rotierenden Refresh-Tokens und Token-Widerruf.
- ownCloud-Verbindung über ein persönliches App-Passwort, verschlüsselt gespeichert.
- Sync-Freigaben je App und Datenart, WebDAV-Dateizugriffe mit ETag-Konfliktschutz.
- Browser-SDK mit lokaler IndexedDB-Arbeitskopie, Offline-Warteschlange,
  ausdrücklicher Konfliktlösung und Trennung verschiedener Konten.
- Kontodatenexport und Kontolöschung. ownCloud-Dateien bleiben dabei erhalten.
- Nginx-Einrichtung mit `certbot certonly --standalone`, Sicherung und Rücknahme
  einer fehlerhaften Proxy-Konfiguration; Wartungsmeldung bis zum App-Start.

### Noch separat einzurichten

Discord benötigt eine eigene OAuth-Anwendung. SMTP braucht einen funktionierenden
Mailzugang. Ein eigenes ownCloud-Konto muss bereits vorhanden sein. Das bestehende
ownCloud-Weblogin wird durch dieses Projekt noch nicht auf SSO umgestellt.
BrickHoard, Unternehmensregister, Startpage und Wetter müssen jeweils an OIDC bzw.
das Sync-SDK angeschlossen werden. Ihre öffentlichen Installationen bleiben bei
der Installation dieser Anwendung unverändert. Ein geprüfter Startpage-Datenadapter
und Einbauhinweise liegen in [integrations/startpage](integrations/startpage/README.md).

## Lokal starten

Python 3.12 oder neuer; auf diesem Server mit Python 3.14 geprüft.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/flask --app wsgi init-secrets
.venv/bin/flask --app wsgi db upgrade
.venv/bin/flask --app wsgi run --host 127.0.0.1 --port 8096
```

`init-secrets` erzeugt `.env` und einen dauerhaften RSA-Schlüssel mit privaten
Dateirechten. Vorhandene Dateien werden nicht überschrieben. Ohne SMTP kann die
Registrierung noch keine Bestätigung versenden. Für ein erstes eigenes Konto:

```bash
.venv/bin/flask --app wsgi create-user julian --email DEINE-ADRESSE --verified
```

Das Passwort wird verdeckt abgefragt. `--verified` nur verwenden, wenn die Adresse
tatsächlich geprüft wurde. Es gibt kein Standardpasswort und kein verstecktes
Administratorkonto. OIDC und die produktive Passkey-Anmeldung über HTTPS testen;
die OIDC-Transportprüfung wird auch lokal nicht abgeschaltet.

## Betrieb auf diesem Server

Die privaten Werte in `.env` behalten und diese Einstellungen ergänzen:

```dotenv
ACCOUNT_ENV=production
ACCOUNT_BASE_URL=https://account.julianverse.de
ACCOUNT_TRUST_PROXY=true
ACCOUNT_REQUIRE_VERIFIED_EMAIL=true
ACCOUNT_REGISTRATION_OPEN=true
OWNCLOUD_BASE_URL=https://cloud.julianverse.de
```

SMTP über SSL auf Port 465 oder STARTTLS auf Port 587 konfigurieren. Für Port 587
`MAIL_USE_SSL=false` und `MAIL_USE_STARTTLS=true` setzen. `MAIL_FROM` muss zu den
Versandberechtigungen des Mailkontos passen. Keine Zugangsdaten ins Repository schreiben.

```bash
bash scripts/install-service.sh
sudo bash scripts/setup-nginx.sh
```

Der App-Dienst läuft als bestehender Benutzer über systemd auf **127.0.0.1:8096**.
Der Benutzer braucht einen laufenden systemd-Benutzermanager mit Linger; auf diesem
Server ist das bereits eingerichtet. `install-service.sh` führt Migrationen aus
und startet den Dienst. Vor späteren Migrationen zuerst eine Sicherung erstellen.

Das Nginx-Skript kann auch zuerst ausgeführt werden: Dann erscheint bis zum
App-Start eine Wartungsmeldung (HTTP 503). Es ergänzt ausschließlich
`/etc/nginx/conf.d/julianverse-account.conf` und speichert die vorherige Konfiguration
unter `/var/backups/julianverse-account-nginx.*`.

Für das Zertifikat wird Nginx kurz gestoppt. In dieser Zeit sind **alle Websites
hinter Nginx** kurz unterbrochen. Das Skript startet Nginx auch nach einem
fehlgeschlagenen Certbot-Aufruf wieder. Die Zertifikat-Hooks liegen unter
`/usr/local/libexec/julianverse-account/` und werden für Erneuerungen gespeichert.
Der vorhandene `certbot-renew.timer` bleibt bestehen.

```bash
systemctl --user status julianverse-account
systemctl --user restart julianverse-account
curl https://account.julianverse.de/healthz
systemctl list-timers --all | grep certbot
# Erneuerungstest verursacht ebenfalls eine kurze Nginx-Unterbrechung:
sudo certbot renew --cert-name account.julianverse.de --dry-run
```

Nginx vertraut keinen vom Client mitgebrachten Proxy-Headern. Flask vertraut
genau dem einen lokalen Proxy. Anfragen werden weder in Gunicorn noch im
Account-Nginx-Access-Log protokolliert, damit Codes und Recovery-Links dort nicht landen.

## Discord

Im Discord Developer Portal eine Anwendung erstellen und diese Redirect-URL eintragen:

```
https://account.julianverse.de/auth/discord/callback
```

`DISCORD_CLIENT_ID` und `DISCORD_CLIENT_SECRET` in `.env` setzen, Dienst neu starten.
Scopes: `identify email`. Kein Bot-Token nötig. Discord-Zugriffstokens werden nur
für den Profilabruf benutzt und nicht in der Datenbank gespeichert. Rollenabgleich,
Bot-Funktionen und Zugriff auf Nachrichten sind nicht Teil dieser Version.

## Apps für SSO registrieren

Für eine Browser-App:

```bash
.venv/bin/flask --app wsgi create-client startpage \
  --name Startpage --public \
  --redirect-uri https://julianverse.de/startpage/account-callback.html \
  --scope 'openid profile email sync'
```

Für eine Flask-App `--public` weglassen. Das erzeugte Client-Secret erscheint
einmalig und wird in der Datenbank ausschließlich als Hash gespeichert.
Jede Redirect-URL muss exakt passen. S256-PKCE ist auch bei vertraulichen Clients
Pflicht. Keine Wildcards, kein Implicit- oder Password-Grant, keine dynamische Registrierung.

| Zweck | URL |
| --- | --- |
| Issuer | `https://account.julianverse.de` |
| Discovery | `/.well-known/openid-configuration` |
| Authorization | `/oauth/authorize` |
| Token | `/oauth/token` |
| Userinfo | `/oauth/userinfo` |
| JWKS | `/oauth/jwks` |
| Token-Widerruf | `/oauth/revoke` |

Scopes: `openid`, `profile`, `email`, `sync`. OIDC-Anfragen enthalten eine zufällige
`nonce` und `state`. Access-Tokens gelten zehn Minuten; Refresh-Tokens werden bei
jeder Nutzung ersetzt und sind höchstens 30 Tage gültig. Wiederverwendung sperrt
die Token-Familie. Der Widerruf einer Kontositzung sperrt deren App-Tokens sofort.
Die eigene Sitzung der angebundenen App muss diese selbst ebenfalls beenden bzw.
die Gültigkeit überprüfen; Front-/Backchannel-Logout ist noch nicht implementiert.

`flask --app wsgi list-clients` zeigt registrierte Apps.
`flask --app wsgi disable-client SLUG` sperrt eine App und ihre Tokens.

## ownCloud und das Dateiformat

Die Verbindung prüft WebDAV mit dem jeweiligen Benutzer und einem App-Passwort.
Ein erneutes Verbinden deaktiviert alle Sync-Freigaben, auch beim Kontowechsel.
Vorhandene App-Tokens mit Sync-Zugriff werden dabei widerrufen, damit ausstehende
lokale Änderungen erst nach erneuter Anmeldung und Aktivierung abgeglichen werden.
Die Dateien liegen im Benutzerbereich, zum Beispiel:

```
Julianverse/startpage/notes.json
Julianverse/startpage/settings.json
Julianverse/weather/locations.json
```

```json
{
  "schemaVersion": 1,
  "data": {"notes": "Meine Notiz"},
  "deleted": false
}
```

Die jeweilige App definiert das Format von `data`. Die maximale Dateigröße beträgt
512 KiB. Dateien lassen sich direkt in ownCloud bearbeiten, solange das Format
erhalten bleibt. Bilder und größere Dateien werden direkt über ownCloud verwaltet.

`GET /api/sync/APP/RESOURCE` liefert JSON und ETag. Ein `PUT` braucht entweder
`If-Match: "gelesene-version"` oder `If-None-Match: *`. Ein Konflikt ergibt 412;
fehlende Schreibbedingungen ergeben 428. Tokens mit Scope `sync` dürfen nur in
den Ordner ihres registrierten App-Slugs, nur für freigegebene Datenarten.

Löschungen werden als Dokument mit `deleted: true` gespeichert. Ein direktes
Löschen in ownCloud löst bei einer vorhandenen lokalen Kopie eine Entscheidung
aus. Ein Netzfehler wird niemals als leere oder gelöschte Datei behandelt.

Das SDK in `account/static/js/sync.mjs` speichert Arbeitskopien und ausstehende
Änderungen in IndexedDB, getrennt nach Issuer, Konto-ID und App. Web Locks verhindern
gleichzeitige Zugriffe mehrerer Tabs. `enable()` erfordert eine ausdrückliche
Auswahl der Datenquelle. `attach()` meldet nur an. `save()` speichert zuerst lokal;
`sync()` gleicht ab. Konflikte bleiben bis `resolve()` erhalten. Der Host muss diese
Zustände anzeigen und darf lokale Fehler nicht durch eine leere Datei ersetzen.

## Sicherung und Wartung

```bash
.venv/bin/flask --app wsgi backup-db /SICHERER/PFAD/account.sqlite3
.venv/bin/flask --app wsgi cleanup
```

`backup-db` verwendet die SQLite-Backup-API und berücksichtigt WAL-Daten. Zusätzlich
`.env` und `instance/oidc-private.pem` geschützt sichern. Ohne den Fernet-Schlüssel
können ownCloud-App-Passwörter und TOTP-Geheimnisse nicht entschlüsselt werden.
ownCloud-Inhalte separat mit der bestehenden ownCloud-Sicherung sichern.

Für eine Wiederherstellung den Dienst stoppen, zusammengehörige Datenbank und
Schlüssel wiederherstellen, Eigentümer/Dateirechte prüfen, `flask db upgrade`
ausführen und neu starten. Schlüsselrotation mit Übergangszeit ist noch nicht
implementiert; vorhandene Schlüssel nicht beiläufig neu erzeugen.

## Entwicklung und Prüfungen

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check account tests
.venv/bin/pytest --ignore=tests/test_browser.py
node --test tests/*.test.mjs
# Optional: echter Browser einschließlich virtuellem WebAuthn-Authenticator
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
.venv/bin/pytest tests/test_browser.py
```

Der Browser braucht die üblichen Systembibliotheken und Fonts. Auf diesem Server
liegen zusätzliche Testbibliotheken ausschließlich im Benutzer-Cache. Tests
verwenden temporäre Datenbanken, einen lokalen TLS-Testserver und gemockte
Discord-/WebDAV-Antworten. Es werden keine echten E-Mails versandt oder Cloud-Dateien geändert.
Der separate Nginx-Test startet einen isolierten Proxy auf Testports.

Neue Schemaänderungen: `flask db migrate -m 'beschreibung'`, Migration prüfen,
dann `flask db upgrade`. Keine Laufzeit-Aufrufe von `db.create_all()`.

Referenzen: [Authlib OIDC](https://docs.authlib.org/en/latest/oauth2/authorization-server/flask/openid-connect.html),
[Discord OAuth2](https://docs.discord.com/developers/topics/oauth2),
[ownCloud WebDAV](https://doc.owncloud.com/server/10.15/developer_manual/webdav_api/index.html),
[Certbot Standalone & Hooks](https://eff-certbot.readthedocs.io/en/stable/using.html).
