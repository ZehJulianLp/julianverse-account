# Julianverse News mit optionalem Account-Sync

Die PWA unter `/news/` nutzt dieselben PKCE-, Browser-Sitzungs- und Sync-Module
wie Startpage und Wetter. **Account & Sync** öffnet den Account-Bereich in der
Übersicht; auf der Leseseite befindet er sich unter **Aa → Account & Sync**.
Der Abgleich läuft auch beim Lesen eines Artikels weiter. Anmeldung und aktivierte
Datenarten bleiben nach dem Neuladen erhalten. Ohne Anmeldung werden keine
Account-Anfragen gestellt; Anmeldung allein aktiviert keinen Sync.

| Freigabe | Inhalt |
| --- | --- |
| `sources` | Feed-Adressen, Namen, Reihenfolge, stabile IDs und pausierte Quellen |
| `saved` | Leseliste einschließlich Titeln, Vorschauen, Links und Merk-Zeitpunkt |
| `read` | Gelesen-Markierungen nach Artikel-URL einschließlich Entfernen der Markierung |
| `settings` | Design, Karten-/Listenansicht, Lesefarbe, Schriftgröße, Schriftart und Textbreite |

Dateien liegen direkt unter `Julianverse/news/` in ownCloud. Jede Freigabe hat eine
eigene JSON-Datei. Beim ersten Abgleich wählt der Nutzer ausdrücklich lokale oder
Cloud-Daten. Cloud-Importe werden vorab geprüft, sichern den bisherigen Stand und
ersetzen nur die ausgewählte Kategorie im bestehenden Nutzerdokument.
Die gemeinsame Speicherdatei und ihr globaler Änderungszeitpunkt führen nicht
zu zusätzlichen Änderungen in anderen Kategorien.

Feed-Cache, vollständige Artikeltexte, Sitzungsdaten, aktuelle Suche, temporäre
Feed-Vorschau und Leseposition werden nicht synchronisiert. Die Leseliste behält
ihre bisherigen Vorschauen. Fremdes Artikel-HTML gehört nicht zum Sync-Dokument.
Die bestehenden lokalen Daten werden bei der Installation nicht verändert.

Der gemeinsame Sync nutzt ETags, erkennt parallele Änderungen und bietet die
bestehende Auswahl zwischen lokaler und Cloud-Version an. Löschungen sind echte
Änderungen der jeweiligen Kategorie; beim Konflikt wird keine Version stillschweigend
überschrieben. News unterstützt bis zu 8 MiB je Datei, damit auch größere Leselisten
und 8.000 Gelesen-Markierungen übertragbar bleiben. Andere Apps behalten ihre
bisherige Grenze von 512 KiB.

## Dateien und Einrichtung

- `adapter.mjs`: Zuordnung und Validierung der vier Datenarten.
- `app.mjs`, `news.css`: Account-Dialog für Übersicht und Leseseite.
- `host.patch`: Anpassungen an der vorhandenen News-PWA (Stand vor Integration: v11).
- `scripts/copy-browser-integration.py news …`: Kopiert das gemeinsame Browser-Modul.
- `scripts/setup-news-proxy.sh`: Richtet die größeren News-Uploads und die Callback-Header ein.

Der öffentliche Client verwendet `openid profile sync` und genau den Callback
`https://julianverse.de/news/account-callback.html`. Kein Client-Secret, keine
zusätzliche E-Mail-Freigabe. Bestehende Nutzer bekommen keine automatischen
Sync-Freigaben. Ein Schema-Update der Account-Datenbank ist nicht nötig.

```bash
.venv/bin/flask --app wsgi create-client news --name 'Julianverse News' \
  --redirect-uri https://julianverse.de/news/account-callback.html \
  --scope 'openid profile sync' --public
# In einem Checkout der bisherigen PWA, einmalig:
git apply --check /home/srvmgr/sso/integrations/news/host.patch
git apply /home/srvmgr/sso/integrations/news/host.patch
# Im Account-Repository:
.venv/bin/python scripts/copy-browser-integration.py news /PFAD/ZUR/NEWS-PWA \
  --client-id=OEFFENTLICHE_CLIENT_ID
systemctl --user reload julianverse-account.service
sudo bash scripts/setup-news-proxy.sh
```

News-Module und HTML vor `sw.js` ausliefern. Der neue Worker v12 nimmt alle
Account-Module in den Offline-Cache auf. OAuth-Callbacks mit Codes umgehen den
Cache und erhalten ihre eigene Seite. Bei einer bereits installierten PWA das
Angebot **Jetzt aktualisieren** annehmen.

Der Nginx-Schritt sichert die beiden betroffenen Konfigurationen, prüft sie und
lädt Nginx neu. Bei einem Fehler stellt er sie wieder her. Er erlaubt 8 MiB nur
unter `/api/sync/news/`; am Callback setzt er `no-store`, `no-referrer` und schaltet
das Zugriffslog aus. Zertifikate und andere Dienste werden nicht neu eingerichtet.
Die Freigabe-Abfrage `/api/sync/news` hat eine eigene exakte Proxy-Regel, damit
Nginx dort keinen abschließenden Slash ergänzt und den CORS-Preflight unterbricht.
Das Skript ergänzt diese Regel auch bei einer bereits eingerichteten News-Anbindung.

## Leichte Prüfungen ohne Browser

```bash
JULIANVERSE_NEWS_CHECKOUT=/PFAD/ZUR/NEWS-PWA node --test tests/news.test.mjs
.venv/bin/pytest -q tests/test_news.py tests/test_cloud.py tests/test_browser_sessions.py
```

Diese Tests starten weder Chromium noch andere Browser. Sie prüfen Datenzuordnung,
Trennung der Kategorien, Löschungen, fehlerhafte Cloud-Daten, Cache-Ausschlüsse,
PKCE, Cookie-Sitzung, Freigaben, ETags und größere News-Dateien in isolierten Tests.
Den abschließenden Browser-/PWA-Test übernimmt der Betreiber.
