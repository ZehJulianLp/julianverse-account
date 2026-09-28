# Startpage mit Julianverse Account

Die statische Startpage nutzt ein lokales JavaScript-Modul für OIDC mit PKCE und
optionalen ownCloud-Sync. Der Account-Bereich sitzt unter **Einstellungen → Account**. Die App lädt
ihre Module lokal und kann ohne Account-Server weiter benutzt werden.

## Bedienung

1. „Einstellungen → Account“ öffnen und „Mit Julianverse anmelden“ wählen.
   Die Anmeldung öffnet ein eigenes Fenster. Der statische Callback gibt den
   einmaligen Code nur an das öffnende Fenster derselben Origin zurück.
2. Unter „Freigaben im Account verwalten“ die gewünschten Datenarten freigeben.
3. Pro Datenart ausdrücklich „Lokale Daten hochladen“ oder „Cloud-Daten übernehmen“
   wählen. Anmeldung allein überträgt keine App-Inhalte.
4. Danach gleicht die geöffnete App lokale Änderungen, bei Rückkehr zur App und
   etwa alle 30 Sekunden ab. Offline-Änderungen bleiben lokal gespeichert.
5. Bei Konflikten beide Versionen herunterladen und die gewünschte Version wählen.
   Vor Cloud-Importen wird die lokale Version gesichert; sie ist im Bereich der
   jeweiligen Datenart als JSON herunterladbar.
6. „Sync ausschalten“ beendet weitere Abgleiche. „App abmelden“ widerruft die
   App-Tokens; lokale Daten und die Anmeldung auf der Account-Seite bleiben erhalten.

Anmeldung und aktivierte Datenarten bleiben nach Neuladen und Browser-Neustart
auf diesem Gerät erhalten. Ein Secure/HttpOnly-Cookie beim Account-Server erneuert
kurzlebige Zugriffstokens, die nur im Arbeitsspeicher liegen. Die Sitzung endet
spätestens nach 30 Tagen oder beim Widerruf der Account-Sitzung. Abmelden und
„Sync ausschalten“ bleiben ebenfalls gespeichert. Offline-Änderungen werden beim
Wiederverbinden mit der bisherigen Dateiversion abgeglichen, damit Konflikte sichtbar bleiben.
Bei einem anderen Account in einem zweiten Tab wird der bisherige Sync gestoppt.

## Datenzuordnung

| Ressource | Lokale Daten |
| --- | --- |
| `notes` | Notizen |
| `tasks` | Aufgaben, Erledigt-Status, Reihenfolge |
| `bookmarks` | Kacheltitel, HTTP(S)-Links und optionale Kachel-IDs (`key`) |
| `settings` | Design, Sprache, sichtbare Widgets, Farben, aktivierte Suchmaschinen |

API-Schlüssel, Agent-Einstellungen, Suchverlauf, Cache, Bilder, Hintergründe und
Profile werden nicht übertragen. Profile enthalten verschachtelte Kopien mehrerer
Datenarten und brauchen eine separate Freigabezuordnung.

Die Dateien liegen in `Julianverse/startpage/` im ownCloud-Konto. Das JSON-Format
muss bei direkter Bearbeitung erhalten bleiben. Die App prüft Struktur, Typen,
Längen und Link-Protokolle vor einer Übernahme. Maximal 512 KiB pro Cloud-Datei.

## Wartung

Die gemeinsame Quelle liegt in `integrations/browser`, das SDK in
`account/static/js`, der Datenadapter hier. Kopien werden bewusst mit der App
ausgeliefert, damit ein Account-Ausfall das Starten der lokalen App nicht verhindert.

```bash
.venv/bin/python scripts/copy-browser-integration.py startpage /PFAD/ZUM/STARTPAGE-CHECKOUT \
  --client-id OEFFENTLICHE_CLIENT_ID
```

Dieser Befehl kopiert Module, öffentliche Konfiguration und Callback in den
Checkout. Er verändert weder HTML-Einbindung noch öffentliche Installation.
Die vorhandene Einbindung nutzt `julianverse:change` nach lokalen Schreibvorgängen
und `julianverseApply` zum Aktualisieren der sichtbaren Widgets.

Der öffentliche Client benötigt die exakte Callback-URL
`https://julianverse.de/startpage/account-callback.html` und die Scopes
`openid profile email sync`. Für andere Installationen müssen Client und
`account/config.mjs` angepasst werden. Anmeldung benötigt HTTPS.

Dauerhafte Anmeldung benötigt Cookies zwischen App und Account auf derselben Site
(wie `julianverse.de` und `account.julianverse.de`). Fremde Sites brauchen eine eigene
Cookie-/Deployment-Lösung; Drittanbieter-Cookies werden nicht vorausgesetzt.
