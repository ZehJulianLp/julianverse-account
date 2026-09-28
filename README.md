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
- Automatische ownCloud-Konten mit 1 GB nach bestätigter E-Mail, mit Wiederholungen
  bei Ausfällen und OIDC-Anmeldung. Sync bleibt zunächst ausgeschaltet.
- Adminbereich für Kontosperren, Adminrechte, Sitzungswiderruf, Passwort-Links
  und den Status der Cloud-Einrichtung.
- Sync-Freigaben je App und Datenart, WebDAV-Dateizugriffe mit ETag-Konfliktschutz.
- Browser-SDK mit lokaler IndexedDB-Arbeitskopie, Offline-Warteschlange,
  ausdrücklicher Konfliktlösung und Trennung verschiedener Konten.
- Kontodatenexport und Kontolöschung. ownCloud-Dateien bleiben dabei erhalten.
- Nginx-Einrichtung mit `certbot certonly --standalone`, Sicherung und Rücknahme
  einer fehlerhaften Proxy-Konfiguration; Wartungsmeldung bis zum App-Start.

### Noch separat einzurichten

Discord benötigt eine OAuth-Anwendung. SMTP braucht einen funktionierenden
Mailzugang. Die eigene ownCloud-Installation wird mit `scripts/setup-owncloud.sh`
angebunden; das Skript ist für den vorhandenen Docker-Compose-Dienst unter
`/opt/owncloud` vorbereitet.
Startpage und Wetter nutzen das gemeinsame Browser-Modul für optionale Anmeldung
und ownCloud-Sync. Datenzuordnung, Bedienung und Einbauhinweise liegen unter
[integrations/startpage](integrations/startpage/README.md) und
[integrations/weather](integrations/weather/README.md). Die Account-Installation
verändert diese statischen Apps nicht automatisch; die Module werden mit den
jeweiligen App-Checkouts ausgeliefert.
BrickHoard und Unternehmensregister benötigen noch ihre eigene OIDC-Anbindung.

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

Das erste Adminrecht wird ausdrücklich vergeben:

```bash
.venv/bin/flask --app wsgi make-admin julian
```

Danach erscheint **Verwaltung** im Menü. Weitere aktive, bestätigte Konten können
dort Adminrechte erhalten. Niemand kann sich dort selbst sperren oder die eigenen
Adminrechte entfernen. Das letzte aktive Adminkonto kann nicht gelöscht werden.

## ownCloud als Grundlage

Nach einer Datenbanksicherung die Account-Version aktualisieren und starten:

```bash
.venv/bin/flask --app wsgi backup-db instance/vor-owncloud.sqlite3
bash scripts/install-service.sh
sudo bash scripts/setup-owncloud.sh
```

Das Skript installiert ownClouds OIDC-App **2.3.5** für ownCloud Server 10.12–10.x
mit fest geprüfter SHA-256-Summe. Es richtet einen zusätzlichen Login-Button und
einen Dienstbenutzer als Gruppenadministrator der Gruppe `julianverse-account`
ein. Er hat keine globalen ownCloud-Adminrechte. Bestehende Konten werden weder
dieser Gruppe hinzugefügt noch verändert. Eine fremde OIDC-Konfiguration wird
nicht überschrieben. Vorherige Login-Konfiguration und Account-Umgebung werden
geschützt unter `/var/backups/julianverse-account-owncloud.*` gesichert.

Die Einrichtung prüft über echtes HTTPS den SSO-Redirect mit PKCE und Nonce sowie
Anlage, 1-GB-Quota, WebDAV-Identität und Sperren/Entsperren mit einem eigens erstellten
leeren Prüfkonto. Nur dieses bestätigte Prüfkonto wird danach entfernt. Bei einem
Fehler im SSO-Schritt wird die vorherige Login-Konfiguration wiederhergestellt.
Ein vollständiger Browser-Login mit einem echten Konto folgt nach der Einrichtung.

Die PHP-Aufrufe laden dieselbe Docker-Konsolenumgebung wie ownClouds eigener
[`occ`-Wrapper](https://github.com/owncloud-docker/base/blob/master/v20.04/overlay/usr/bin/occ).
Nur Container und Konfiguration prüfen, ohne die Einrichtung zu beginnen:

```bash
sudo bash scripts/setup-owncloud.sh --check
```

Bei fehlgeschlagenen Befehlen nennt das Skript Schritt und Exit-Code. Vollständige
Fehlerausgaben bleiben in einer nur für root lesbaren Datei unter
`/var/log/julianverse-owncloud-error-*.log`; die Terminalmeldung enthält keine
Zugangsdaten.

### Neue Benutzer

- Passwortregistrierungen erhalten sofort einen gespeicherten Cloud-Auftrag;
  die Anlage beginnt erst nach bestätigter E-Mail. Dasselbe gilt für per CLI
  erstellte Konten. Neue Discord-Konten besitzen bereits eine bestätigte E-Mail.
- `julianverse-cloud.timer` bearbeitet Aufträge etwa jede Minute. Fehler sind im
  Adminbereich sichtbar und werden automatisch erneut versucht.
- Neue Cloud-Namen sind aus der unveränderlichen Account-ID abgeleitet. Das
  vermeidet Kollisionen mit bestehenden Benutzernamen. Im Web wird der Anzeigename
  verwendet. Das zufällige Cloud-Passwort liegt ausschließlich verschlüsselt vor.
- Die Quota wird in ownCloud auf **1 GB (1.073.741.824 Bytes)** gesetzt und geprüft,
  bevor SSO oder die Sync-API Zugriff erhalten. Größere Dateien und Bilder lädt
  der Nutzer direkt in ownCloud hoch. Diese Quota umfasst die eigenen Dateien;
  Vorschaubilder, Versionen und Papierkorb können zusätzlich Serverplatz belegen.
- Die automatische Einrichtung aktiviert keine Sync-Freigabe und lädt keine
  lokalen App-Daten hoch. Unter Verbindungen führt **Mit Julianverse anmelden**
  zum eigenen Cloud-Konto. In ownCloud lassen sich App-Passwörter für Clients anlegen.

### Bestehende Benutzer migrieren

1. Bei der Passwortregistrierung **Ich habe bereits ein ownCloud-Konto** wählen.
   Dadurch wird kein zweites Cloud-Konto erzeugt. Bei der CLI lautet die Option
   `--existing-cloud`. Bereits vorhandene Julianverse-Konten erhalten ebenfalls
   keinen automatischen zusätzlichen Cloud-Auftrag.
2. Zunächst mit dem bisherigen Login in ownCloud anmelden und ein persönliches
   App-Passwort erstellen.
3. In Julianverse Account unter **Verbindungen → ownCloud** Benutzername und
   App-Passwort bestätigen. Die App liest den authentifizierten WebDAV-Principal
   und ordnet dieses Cloud-Konto genau einem Julianverse-Konto zu. Gleiche E-Mails
   oder ähnliche Benutzernamen reichen ausdrücklich nicht aus.
4. Danach SSO ausprobieren. Dateien, Freigaben, Kontoname und bisherige Quota
   bleiben bestehen. Anschließend kann Discord zusätzlich verknüpft werden.

Für bestehende Cloud-Nutzer ist dieser Weg vor einer neuen Discord-Registrierung
vorgesehen, da diese sonst automatisch ein neues Cloud-Konto anlegt. Ein bereits
angelegtes verwaltetes Konto lässt sich nicht stillschweigend gegen ein anderes
tauschen; vorhandene Dateien müssen bei einem späteren Wechsel separat berücksichtigt
werden. Eine Übertragung von Dateien ist nicht Teil der Kontoverknüpfung.

### Sperren, Sitzungen und Wiederherstellung

Kontosperren widerrufen Julianverse-Sitzungen und App-Zugriffe sofort. Bei neu
angelegten, verwalteten Cloud-Konten sperrt der Hintergrunddienst zusätzlich den
ownCloud-Benutzer. Bis zur erfolgreichen Ausführung zeigt die Verwaltung die
Änderung als ausstehend an. Selbst verknüpfte Bestandskonten behalten ihren bisherigen
Cloud-Login; dessen Sperre bleibt eine Aufgabe der ownCloud-Verwaltung.
ownCloud prüft SSO-Tokens über Introspection und bei der Erneuerung. Bereits dort
zwischengespeicherte SSO-Sitzungen können bis zur nächsten Prüfung weiterbestehen
(bei den aktuellen Tokens bis zu zehn Minuten). Das Beenden einer einzelnen
Julianverse-Sitzung sperrt keine separaten ownCloud-App-Passwörter.

Bei einer Kontolöschung wird ein verwalteter Cloud-Zugang zuerst gesperrt. Seine
Dateien werden nicht entfernt. Vorher herunterladen; spätere Wiederherstellung
erfordert den Betreiber. Verknüpfte Bestandskonten bleiben eigenständig nutzbar.

```bash
systemctl --user status julianverse-cloud.timer
.venv/bin/flask --app wsgi reconcile-cloud
```

`instance/owncloud-setup.json` enthält verschlüsselte Einrichtungsschlüssel und
gehört zusammen mit `.env` und der Datenbank in die private Sicherung. Für einen
Rückbau zuerst den Cloud-Timer stoppen, dann die gesicherte `openid-connect`-
Konfiguration mit ownClouds Konfigurationswerkzeugen wiederherstellen und die
passende Account-Umgebung zurückspielen. Angelegte Benutzer und deren Dateien
werden durch einen Rückbau nicht automatisch gelöscht.

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

Die statischen Apps merken Anmeldung und aktivierte Datenarten pro Konto auf dem
Gerät. `POST /oauth/browser/APP` tauscht nach PKCE ein Zugriffstoken gegen eine
App-Sitzung mit Secure/HttpOnly-Cookie und erneuert kurzlebige Zugriffstokens.
Nur exakt registrierte Origins mit eigenem CSRF-Header dürfen diesen Endpunkt
mit Cookies aufrufen. Die App-Sitzung bleibt an die ursprüngliche Kontositzung
und Token-Familie gebunden, höchstens 30 Tage. Widerruf und Kontosperre wirken sofort.
Tokens werden nicht in localStorage oder IndexedDB abgelegt. Dort bleiben nur
Kontoinformationen, Sync-Auswahl, Prüfsummen und lokale Arbeitskopien.
Anmeldung allein aktiviert keine neue Datenart. Bereits ausdrücklich aktivierte
Datenarten werden wieder aufgenommen; alte ETags schützen auch vor Konflikten
nach Offline-Änderungen oder einem Neuladen. „Sync ausschalten“ und Abmelden bleiben
gespeichert. Ein offline ausgelöstes Abmelden stoppt sofort lokal und widerruft
die App-Sitzung beim nächsten Kontakt. Andere Tabs derselben App werden gestoppt.
App und Account müssen für diese Cookies auf derselben Site liegen.

WebDAV verwendet `Accept-Encoding: identity`, damit Lesen und bedingtes Schreiben
dieselbe Dateiversion verwenden. Der Inhaltsvergleich ignoriert die Reihenfolge
von JSON-Objektschlüsseln, berücksichtigt aber die Reihenfolge in Arrays. Alte
Konflikte verschwinden automatisch, wenn beide Inhalte gleich sind oder sich
nachweislich nur die bekannte Apache-`-gzip`-Kennung derselben ownCloud-Version
unterscheidet. Andere Versionskonflikte erfordern weiterhin eine Auswahl.

Lesende ownCloud-Anfragen werden bei einem Transportfehler oder HTTP 502/503/504
einmal wiederholt. Unsichere Schreibwiederholungen bleiben ausgeschlossen.

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
[ownCloud Provisioning API](https://doc.owncloud.com/server/10.15/developer_manual/core/apis/provisioning-api.html),
[ownCloud OIDC-App](https://github.com/owncloud/openidconnect/releases/tag/v2.3.5),
[Certbot Standalone & Hooks](https://eff-certbot.readthedocs.io/en/stable/using.html).
