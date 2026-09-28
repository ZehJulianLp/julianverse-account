# Wetter mit Julianverse Account

Der Account-Bereich befindet sich in der Seitenleiste unter den gespeicherten Orten.
Anmeldung, Freigaben, Upload/Download und Konfliktauflösung verwenden dasselbe
Browser-Modul wie die [Startpage](../startpage/README.md).

- `settings`: Sprache, Design, Layout, Einheiten, Aktivität, Diagramme und Module.
- `locations`: ausdrücklich gespeicherte Orte, Reihenfolge und Standardort.

Aktueller Gerätestandort, letzter aufgerufener Ort, Wetter-Cache, Benachrichtigungs-
und Installationszustand bleiben lokal. Ein ausdrücklich gespeicherter Ort enthält
seine Koordinaten. Benachrichtigungen werden auf einem anderen Gerät nicht aktiviert.

Dateien liegen unter `Julianverse/weather/` in ownCloud. Cloud-Importe aktualisieren
localStorage, die zusätzliche IndexedDB-Kopie und die sichtbaren Einstellungen.
Die PWA hält die Account-Module lokal im Cache; Auth-Callbacks mit Anmeldecodes
werden nicht gecacht. Nach einem Neuladen bleiben lokale Daten erhalten, während
Anmeldung und Sync-Auswahl erneut nötig sind.

```bash
.venv/bin/python scripts/copy-browser-integration.py weather /PFAD/ZUM/WETTER-CHECKOUT \
  --client-id OEFFENTLICHE_CLIENT_ID
```

Client-Callback: `https://julianverse.de/weather/account-callback.html`.
Scopes: `openid profile email sync`, öffentlicher Client ohne Client-Secret.
Bei Änderungen an gecachten App-Dateien die Cache-Version in `sw.js` erhöhen.

## Browserprüfung beider Apps

```bash
JULIANVERSE_STARTPAGE_CHECKOUT=/PFAD/ZUR/STARTPAGE \
JULIANVERSE_WEATHER_CHECKOUT=/PFAD/ZUM/WETTER \
.venv/bin/pytest -q tests/test_static_apps.py
node --test tests/*.test.mjs
```

Der Browser-Test verwendet zwei lokale HTTPS-Server mit echter Account-Anmeldung,
PKCE, CORS, Token-Rotation und Sync-API. ownCloud-Dateien werden im Test nur im
Arbeitsspeicher gehalten. Bestehende Nutzerkonten und Cloud-Dateien bleiben unberührt.
