# Startpage anbinden

Der Adapter ist für die vorhandene Startpage unter `/srv/http/startpage` vorbereitet.
Die öffentliche Startpage wird durch die Account-Installation **noch nicht verändert**.
Die vorhandenen Apps brauchen zusätzlich ihre eigenen Sync-Bedienelemente und einen
Anmelde-Callback. Der Account-Server und das Browser-SDK sind dafür implementiert.

## Datenzuordnung

| Ressource | Vorhandene lokale Daten |
| --- | --- |
| `notes` | `notes` |
| `tasks` | `todos` |
| `bookmarks` | `tiles` |
| `settings` | explizite Liste von Darstellung, Sprache, Widgets, Suchmaschinen |

API-Schlüssel, Agent-Einstellungen, Suchverlauf, Cache und Bilder werden nicht erfasst.
Profile enthalten in der bestehenden App verschachtelte Kopien vieler Datenarten;
dafür braucht es vor der Integration eine eigene Zuordnung der Freigaben. Der erste
Adapter synchronisiert deshalb keine Profile.

## Einbindung

1. `adapter.mjs`, `account/static/js/sync.mjs` und `oidc-client.mjs` als lokale Dateien
   in die Startpage übernehmen. Dadurch bleibt die lokale App auch bei einem Ausfall
   des Account-Servers startbar.
2. Einen öffentlichen OIDC-Client mit Slug `startpage`, exakter HTTPS-Callback-URL
   und Scopes `openid profile email sync` anlegen (siehe Haupt-README).
3. „Mit Julianverse anmelden“ löst `beginLogin()` aus. Der Callback verwendet
   `finishLogin()`. Tokens bleiben im Arbeitsspeicher; bei einem Neuladen kann der
   Nutzer erneut anmelden, ohne lokale Daten zu verlieren.
4. Nach `sync.attach(tokens.access_token)` bleibt Sync aus. Pro Ressource ausdrücklich
   Cloud-Daten übernehmen, lokale Daten hochladen oder einen gespeicherten
   Arbeitsstand fortsetzen lassen. Vor dem ersten Abgleich die lokale Version sichern.
5. Bei Änderungen zuerst die bestehende lokale Speicherung ausführen, dann
   `sync.save(resource, snapshot(resource))`. Ein fehlgeschlagener Abgleich darf
   die lokale Bearbeitung nicht blockieren.
6. `sync.sync(resource)` nur für aktivierte Ressourcen aufrufen. Bei `record.conflict`
   beide Versionen anzeigen und `resolve(resource, 'local' | 'cloud')` erst nach
   einer Entscheidung ausführen. Änderungen aus der Cloud vor `apply()` in der App
   validieren (Typen, Längen, Link-Protokolle); danach die betroffenen Widgets neu rendern.
7. `stop(resource)` oder `disconnect()` stoppt den Abgleich. Lokale Arbeitskopien
   bleiben erhalten. Der Account-Wechsel aktiviert keine der vorherigen Ressourcen.

```js
import {JulianverseSync} from './sync.mjs';
import {snapshot} from './adapter.mjs';

const sync = new JulianverseSync({issuer: 'https://account.julianverse.de', app: 'startpage'});
await sync.attach(tokens.access_token);
// Erst nach ausdrücklicher Auswahl „Diese lokalen Notizen hochladen“:
await sync.enable('notes', {source: 'local', localData: snapshot('notes')});
const record = await sync.sync('notes');
// record.conflict muss in der App behandelt werden.
```

`enable()` lädt selbst keine Inhalte hoch. Erst `sync()` führt einen bedingten
Schreibzugriff aus. Offline geänderte Daten bleiben in IndexedDB erhalten.
