# Julianverse App SDK und statische Vorlage

Die ZIP-Datei aus **Meine Apps** enthält öffentliche App-Einstellungen,
einen lokalen JSON-Editor, den Account-Bereich und alle Module. Keine Installation,
kein Build und kein Client-Secret nötig. Die Dateien bilden die registrierten
URL-Pfade ab: Inhalt im Webroot der registrierten Domain veröffentlichen.
Für einen Callback `account-callback.html` wird die passende HTML-Seite mitgeliefert.
Ein Callback ohne `.html` muss vom Host ausdrücklich als `text/html` ausgeliefert werden.

## Entwickeln

1. App-Adresse und Callback im Account eintragen. Für localhost ist im Testmodus
   HTTP erlaubt, zum Beispiel `http://localhost:8000/` und
   `http://localhost:8000/account-callback.html`.
2. Bis zu zehn Datenarten mit eigenen Titeln und Beschreibungen anlegen.
3. Vorlage herunterladen und mit einem statischen Server starten, etwa
   `python3 -m http.server 8000 --bind 127.0.0.1` im entpackten Webroot.
4. Der Testmodus erlaubt nur dem App-Ersteller eine Anmeldung. Für andere Nutzer
   „Per Link teilbar“ wählen und den Testmodus ausschalten. Danach neu anmelden.
5. JSON-Editor in `app.mjs` durch die eigene Oberfläche ersetzen.

```js
import { createJSONAdapter, mountAccount } from './account/sdk.mjs';
import { config } from './account/config.mjs';

const data = createJSONAdapter(config);
data.set('notes', { text: 'Meine Notiz' }); // zuerst lokal; kein erzwungener Upload
const notes = data.get('notes');
mountAccount({
  root: document.querySelector('#account'),
  config,
  adapter: data,
  onApply: resource => render(resource, data.get(resource)),
});
```

`account/panel.css` einbinden. Jeder Schlüssel in `config.resources` entspricht
einer im Account registrierten Datenart. Nach Änderungen die Konfiguration
aktualisieren oder die Vorlage neu herunterladen. `set()` meldet Änderungen
an das Panel; `onApply` aktualisiert deine Oberfläche nach einem Cloud-Import.

Ohne Anmeldung bleibt die App lokal nutzbar. Anmeldung aktiviert keinen Sync.
Jeder Nutzer erteilt im Account eine Freigabe pro Datenart und wählt in der App
ausdrücklich die erste Übertragungsrichtung. Die Dateien liegen in seiner ownCloud
unter `Julianverse/APP-ID/DATENART.json` und zählen zu seinem Speicherlimit.
Pro Datei sind höchstens 512 KiB inklusive JSON-Umschlag erlaubt. Fehler beim
Hochladen verwerfen keine lokale Änderung.

## Eigenes Speicherformat

`mountAccount` nimmt auch eigene Adapter entgegen. Implementiere:

- `resources`: Array der registrierten Schlüssel.
- `keys`: Zuordnung von Datenarten zu beobachteten localStorage-Schlüsseln.
- `labels`: Titel je Schlüssel unter `de` und `en`.
- `snapshot(resource)`: Aktuelle lokale Daten als JSON-Objekt.
- `validate(resource, document)`: Cloud-Dokument vor dem Import prüfen.
- `backup(resource)` und `backupKey(resource)`: Lokale Sicherung erstellen und benennen.
- `apply(resource, document)`: Nur diese Datenart ersetzen; andere Daten erhalten.

Änderungen über `new CustomEvent('julianverse:change', {detail: {key}})` auf `window`
melden. Immer zuerst lokal speichern. Das SDK exportiert außerdem
`JulianverseSync`, `IndexedDBStore`, `SyncError`, `beginLogin` und `finishLogin`
für eigene Oberflächen.

## Anmeldung, Konflikte und Hosting

- PKCE/S256, exakte Callback-Adresse, Konto-ID über den authentifizierten
  Userinfo-Endpunkt. Access-Tokens bleiben im Arbeitsspeicher.
- Wiederherstellung der Anmeldung über ein sicheres HttpOnly-Cookie.
  Browser können Drittanbieter-Cookies auf externen Domains blockieren.
  Dann nach Neuladen oder Ablauf des kurzlebigen Zugangs erneut den Anmeldebutton verwenden; das bestehende Account-Login kann
  weiterverwendet werden. Lokale Daten und Sync-Auswahl bleiben erhalten.
- ETags verhindern unbemerktes Überschreiben. Bei parallelen Änderungen zeigt
  das Panel beide Versionen und lässt den Nutzer wählen. Offline-Änderungen
  bleiben lokal und werden nach Wiederverbindung abgeglichen.
- Callbacks mit `Cache-Control: no-store` und `Referrer-Policy: no-referrer`
  ausliefern, Zugriffslogs dort ausschalten und Service-Worker-Caches umgehen.
  App und Callback müssen dieselbe Origin haben. Keine Wildcards.
- Für fremden App-Code eigene Origins verwenden. Diese Vorlage wird selbst
  gehostet; Julianverse Account hostet keine hochgeladenen Webapps.

## Kleine Tests

Im Account-Repository: `node --test tests/sdk.test.mjs` und
`.venv/bin/pytest -q tests/test_developer.py`. Diese Tests starten keinen Browser.
