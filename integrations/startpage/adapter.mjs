/** Allowlisted mapping for the existing vanilla-JS Startpage localStorage format. */
const keys = {
  notes: ["notes"],
  tasks: ["todos"],
  bookmarks: ["tiles"],
  settings: [
    "theme",
    "widgets",
    "widget.colors",
    "engines.enabled",
    "ui.locale",
    "ui.cardStyle",
    "ui.clock.color",
    "ui.search.color",
    "ui.accent.color",
    "ui.modal.color",
    "ui.button.color",
    "ui.input.color",
  ],
};

export const resources = Object.keys(keys);

function allowed(resource) {
  if (!keys[resource])
    throw new Error(
      "Diese Startpage-Daten werden vom Adapter noch nicht unterstützt.",
    );
  return keys[resource];
}

export function snapshot(resource, storage = localStorage) {
  return Object.fromEntries(
    allowed(resource)
      .filter((key) => storage.getItem(key) !== null)
      .map((key) => [key, JSON.parse(storage.getItem(key))]),
  );
}

export function apply(resource, document, storage = localStorage) {
  const permitted = allowed(resource);
  if (
    !document ||
    document.schemaVersion !== 1 ||
    typeof document.data !== "object" ||
    Array.isArray(document.data) ||
    document.data === null
  )
    throw new Error("Die Startpage-Datei hat ein ungültiges Format.");
  const data = document.deleted ? {} : document.data;
  if (Object.keys(data).some((key) => !permitted.includes(key)))
    throw new Error(
      "Die Datei enthält fremde oder nicht freigegebene Einstellungen.",
    );
  // Download/retain this return value before the caller offers to apply the next cloud copy.
  const previous = snapshot(resource, storage);
  const backupKey = `julianverse.backup.${resource}`;
  storage.setItem(backupKey, JSON.stringify(previous)); // Must succeed before replacing local values.
  for (const key of permitted) {
    if (key in data) storage.setItem(key, JSON.stringify(data[key]));
    else storage.removeItem(key);
  }
  return previous;
}
