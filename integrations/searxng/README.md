# SearXNG mit Julianverse Account

`https://search.julianverse.de/julianverse/` ergänzt SearXNG um „Meine Suche“.
Die normale Suche bleibt ohne Konto und ohne JavaScript nutzbar. Ein Link erscheint
auf Startseite, Ergebnisseiten und in den SearXNG-Einstellungen.

## Daten und Bedienung

- **Gespeicherte Suchen:** Suchbegriff, optionaler Name, Kategorien, Sprache,
  Zeitraum und SafeSearch. Speichern auf Ergebnisseiten oder unter „Meine Suche“,
  umbenennen, entfernen und erneut suchen. Bis zu 200 Einträge.
- **Verlauf:** standardmäßig aus, pro Gerät aktivieren oder pausieren. Erfasst nur
  Suchbegriffe und Filter, keine angeklickten Ergebnisse. Einzelne Einträge oder
  den gesamten Verlauf löschen. Aufbewahrung 7, 30 (Standard), 90 Tage oder ohne
  Zeitlimit; höchstens 200 Einträge. Ablauf wird beim Öffnen und minütlich bei
  sichtbarer Seite geprüft. Bei geschlossener App läuft kein Löschdienst.
- **Sucheinstellungen:** Sprache, Darstellung, Kategorien, SafeSearch,
  Autovervollständigung, Ergebnisverhalten sowie Auswahl von Suchmaschinen und
  Plugins. Bearbeitung weiterhin in `/preferences`. Eine feste Cookie-Liste
  schließt Engine-Zugangstoken, Anmeldecookies und kodierte Einstellungs-URLs aus.
  Cloud-Änderungen gelten für die nächste Suche; geöffnete native Einstellungsformulare
  können mit einem Neuladen aktualisiert werden.

Alle Daten sind zunächst lokal. Anmeldung aktiviert keinen Sync. Der öffentliche
PKCE-Client benötigt nur `openid profile sync`; weder E-Mail noch Client-Secret.
Für jede Datenart sind Account-Freigabe und eine ausdrückliche erste Übertragung
erforderlich. Anmeldung und Sync-Auswahl bleiben mit der vorhandenen Browser-Sitzung
erhalten. Tokens werden nicht in localStorage gespeichert.

Die maßgeblichen Cloud-Dateien sind:

```text
Julianverse/searxng/favorites.json
Julianverse/searxng/history.json
Julianverse/searxng/settings.json
```

Die Erlaubnis, neue Suchanfragen im Verlauf zu erfassen, bleibt gerätebezogen und
wird beim Import nicht aktiviert. Löschungen werden bei aktivem Sync beim nächsten
erfolgreichen Abgleich in die aktuelle Cloud-Datei übernommen. Offline-Änderungen
bleiben lokal. Gleichzeitig geänderte Dateien erfordern eine ausdrückliche Auswahl;
der bestehende Sync verwendet ownCloud-ETags und überschreibt keine neuere Version
unbemerkt. Alte ownCloud-Dateiversionen und externe Backups folgen deren Aufbewahrung.

## Installation

Als `srvmgr` im Account-Repository:

```bash
.venv/bin/python scripts/build-searxng-integration.py --register
systemctl --user reload julianverse-account.service
sudo bash scripts/setup-searxng.sh
```

Der Build registriert einen öffentlichen Client mit genau diesem Callback:
`https://search.julianverse.de/julianverse/account-callback.html`.
Ein bestehender Client wird geprüft und wiederverwendet. Es werden keine neuen
Sync-Freigaben für Nutzer gesetzt. Kein Datenbankschema-Update ist nötig.

Das Installationsskript ergänzt ausschließlich den vorhandenen HTTPS-Proxy für
`search.julianverse.de` in `/etc/nginx/nginx.conf`:

- Öffentliche statische Dateien nach `/srv/http/searxng-account/` kopieren.
- HTML-Antworten über Nginx `sub_filter` um das lokale Modul und CSS ergänzen.
- `/julianverse/` mit korrekten MIME-Typen, `no-store`, CSP und `no-referrer` ausliefern.
- Zugriffslogs für den HTTPS-Suchserver deaktivieren, Fehlerprotokoll auf `crit`
  begrenzen, damit Suchbegriffe und OAuth-Callback-Codes nicht in normalen Logs stehen.
- Nginx-Konfiguration prüfen, neu laden und öffentliche Seiten/Assets prüfen.

SearXNG-Container, Engine-Konfiguration und Zertifikate werden nicht verändert.
Der Container muss nicht neu starten. Konfiguration und vorhandene Assets werden
unter `/var/backups/julianverse-search.*` gesichert. Fehlgeschlagene Aktivierung
stellt den bisherigen Proxy wieder her. Wiederholtes Ausführen ist unterstützt;
unerwartete vorhandene Filter/Upstreams führen zum Abbruch vor Änderungen.

Prüfung nach der Installation:

```bash
python3 scripts/setup-searxng.py --check
```

## Entwicklung und Prüfungen

```bash
.venv/bin/python scripts/build-searxng-integration.py --client-id TEST --output /tmp/search-preview
node --test tests/searxng.test.mjs
.venv/bin/pytest -q tests/test_searxng_setup.py tests/test_searxng_browser.py
```

Der Browser-Test nutzt echte Account-Anmeldung, PKCE, Sitzungscookies und Sync-API
mit temporären Konten und ownCloud-Dateien im Arbeitsspeicher. Er prüft getrennte
Freigaben/Aktivierung, Verlaufspause, Löschung, Suchfilter, sichere Textdarstellung,
Cookie-Übernahme, Anmeldung nach Neuladen, automatischen Sync nach einer Suche,
Offline-Konflikte, Abmelden und vier Bildschirmbreiten.

Die Cookie-Darstellung wurde gegen die installierte SearXNG-Version
`2026.9.17+c49771992` geprüft. Nach einem SearXNG-Update Startseite, Ergebnisseite
und Einstellungen prüfen: die Integration verwendet `meta[name=endpoint]`,
`#links_on_top` und `form#search`. Fehlende Integration blockiert die Suche nicht.
