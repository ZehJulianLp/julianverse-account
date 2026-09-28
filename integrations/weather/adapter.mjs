import {
  assert,
  isObject,
  isText,
  documentData,
  readKeys,
  replaceKeys,
} from "../browser/data.mjs";
const prefix = "julianverse-weather:";
export const keys = {
  settings: [prefix + "settings"],
  locations: [prefix + "saved-locations", prefix + "pinned-location"],
};
export const resources = Object.keys(keys);
export const labels = {
  de: {
    settings: "Einstellungen",
    locations: "Gespeicherte Orte & Standardort",
  },
  en: { settings: "Settings", locations: "Saved & default places" },
};
const settings = {
  language: ["de", "en"],
  theme: ["dark", "light"],
  layout: ["expanded", "compact"],
  units: ["metric", "imperial"],
  activity: ["general", "walking", "cycling", "running", "garden", "photo"],
};
const flags = {
  charts: ["temperature", "precipitation", "wind", "uv"],
  modules: [
    "current",
    "overview",
    "nowcast",
    "rainMap",
    "recommendation",
    "signals",
    "hourly",
    "charts",
    "daily",
  ],
};
function place(value) {
  assert(
    isObject(value) && isText(value.name, 500) && isText(value.timezone, 100),
  );
  assert(
    Object.keys(value).every((k) =>
      ["name", "latitude", "longitude", "timezone"].includes(k),
    ),
  );
  assert(Number.isFinite(value.latitude) && Math.abs(value.latitude) <= 90);
  assert(Number.isFinite(value.longitude) && Math.abs(value.longitude) <= 180);
  try {
    if (value.timezone !== "auto")
      new Intl.DateTimeFormat("en", { timeZone: value.timezone });
  } catch {
    assert(false);
  }
}
export function validate(resource, document) {
  const data = documentData(document);
  assert(
    Object.hasOwn(keys, resource) &&
      Object.keys(data).every((k) => keys[resource].includes(k)),
  );
  if (resource === "settings" && Object.hasOwn(data, keys.settings[0])) {
    const value = data[keys.settings[0]];
    assert(isObject(value));
    for (const [key, v] of Object.entries(value)) {
      if (Object.hasOwn(settings, key)) assert(settings[key].includes(v));
      else {
        assert(Object.hasOwn(flags, key) && isObject(v));
        assert(
          Object.keys(v).every((k) => flags[key].includes(k)) &&
            Object.values(v).every((b) => typeof b === "boolean"),
        );
      }
    }
  }
  if (resource === "locations") {
    const list = data[keys.locations[0]];
    if (list !== undefined) {
      assert(Array.isArray(list) && list.length <= 500);
      list.forEach(place);
    }
    const pinned = data[keys.locations[1]];
    if (pinned !== undefined && pinned !== null) place(pinned);
  }
  return data;
}
export function snapshot(resource, storage = localStorage) {
  assert(Object.hasOwn(keys, resource));
  const data = readKeys(keys[resource], storage);
  // Notification preferences and permissions are specific to this device.
  if (data[keys.settings[0]]) {
    data[keys.settings[0]] = Object.fromEntries(
      Object.entries(data[keys.settings[0]]).filter(
        ([key]) => Object.hasOwn(settings, key) || Object.hasOwn(flags, key),
      ),
    );
  }
  validate(resource, { schemaVersion: 1, data });
  return data;
}
export const backupKey = (resource) => `${prefix}sync-backup:${resource}`;
export function backup(resource, storage = localStorage) {
  const previous = snapshot(resource, storage);
  storage.setItem(backupKey(resource), JSON.stringify(previous));
  return previous;
}
export function apply(resource, document, storage = localStorage) {
  const data = structuredClone(validate(resource, document));
  const previous = backup(resource, storage);
  if (resource === "settings") {
    const local = JSON.parse(storage.getItem(keys.settings[0]) || "{}");
    data[keys.settings[0]] = {
      ...data[keys.settings[0]],
      rainNotifications: local.rainNotifications === true,
    };
  }
  replaceKeys(keys[resource], data, storage);
  return previous;
}
