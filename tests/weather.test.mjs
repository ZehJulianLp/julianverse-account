import test from "node:test";
import assert from "node:assert/strict";
import * as weather from "../integrations/weather/adapter.mjs";
import * as startpage from "../integrations/startpage/adapter.mjs";
class Storage {
  data = new Map();
  getItem(key) {
    return this.data.get(key) ?? null;
  }
  setItem(key, value) {
    this.data.set(key, value);
  }
  removeItem(key) {
    this.data.delete(key);
  }
}
const document = (data) => ({ schemaVersion: 1, data, deleted: false });
test("weather sync excludes current GPS location, forecast cache and notification settings", () => {
  const storage = new Storage();
  for (const key of ["last-location", "last-weather", "rain-notifications"])
    storage.setItem(
      `julianverse-weather:${key}`,
      JSON.stringify({ private: true }),
    );
  storage.setItem(
    weather.keys.settings[0],
    JSON.stringify({ theme: "dark", rainNotifications: true }),
  );
  assert.deepEqual(weather.snapshot("settings", storage), {
    [weather.keys.settings[0]]: { theme: "dark" },
  });
  assert.deepEqual(weather.snapshot("locations", storage), {});
  weather.apply(
    "settings",
    document({ [weather.keys.settings[0]]: { theme: "light" } }),
    storage,
  );
  assert.deepEqual(JSON.parse(storage.getItem(weather.keys.settings[0])), {
    theme: "light",
    rainNotifications: true,
  });
  assert.equal(
    JSON.parse(storage.getItem(weather.backupKey("settings")))[
      weather.keys.settings[0]
    ].theme,
    "dark",
  );
});
test("invalid weather coordinates, setting values and unrelated keys never modify local data", () => {
  const storage = new Storage();
  const place = {
    name: "Hannover",
    latitude: 52.37,
    longitude: 9.73,
    timezone: "auto",
  };
  const initial = { [weather.keys.locations[0]]: [place] };
  weather.apply("locations", document(initial), storage);
  for (const bad of [
    { [weather.keys.locations[0]]: [{ ...place, latitude: 999 }] },
    { [weather.keys.locations[0]]: [{ ...place, timezone: "not-a-timezone" }] },
    { "julianverse-weather:last-location": place },
    { [weather.keys.locations[0]]: "bad" },
  ]) {
    assert.throws(() => weather.apply("locations", document(bad), storage));
    assert.deepEqual(weather.snapshot("locations", storage), initial);
  }
  assert.throws(() =>
    weather.apply(
      "settings",
      document({ [weather.keys.settings[0]]: { rainNotifications: true } }),
      storage,
    ),
  );
  assert.throws(() =>
    weather.apply(
      "settings",
      document({ [weather.keys.settings[0]]: { theme: "<script>" } }),
      storage,
    ),
  );
});
test("Startpage rejects executable bookmark URLs and malformed tasks before backup or write", () => {
  const storage = new Storage();
  storage.setItem(
    "tiles",
    JSON.stringify([{ title: "Julianverse", url: "https://julianverse.de" }]),
  );
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,test",
    "https://user:secret@example.org",
  ])
    assert.throws(() =>
      startpage.apply(
        "bookmarks",
        document({ tiles: [{ title: "x", url }] }),
        storage,
      ),
    );
  assert.equal(storage.getItem(startpage.backupKey("bookmarks")), null);
  assert.throws(() =>
    startpage.apply(
      "tasks",
      document({ todos: [{ text: [], done: false }] }),
      storage,
    ),
  );
  assert.throws(() =>
    startpage.apply(
      "settings",
      document({ widgets: { "<x>": true } }),
      storage,
    ),
  );
});
