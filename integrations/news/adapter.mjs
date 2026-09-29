import { assert, isObject, isText, documentData } from "../browser/data.mjs";

export const STATE_KEY = "julianverse.news.state.v1";
export const resources = ["sources", "saved", "read", "settings"];
export const keys = Object.fromEntries(
  resources.map((resource) => [resource, [STATE_KEY]]),
);
export const labels = {
  de: {
    sources: "Quellen & Reihenfolge",
    saved: "Leseliste",
    read: "Gelesen-Status",
    settings: "Ansicht & Leseeinstellungen",
  },
  en: {
    sources: "Sources & order",
    saved: "Reading list",
    read: "Read status",
    settings: "View & reading preferences",
  },
};
const preferenceValues = {
  theme: ["auto", "light", "dark"],
  layout: ["grid", "list"],
  readingTheme: ["auto", "light", "dark", "sepia"],
  readingFont: ["sans", "serif"],
  readingWidth: ["normal", "wide"],
};
const sourceFields = ["id", "url", "name", "enabled", "updatedAt"];
const articleFields = [
  "id",
  "link",
  "title",
  "summary",
  "image",
  "publishedAt",
  "sourceId",
  "sourceName",
  "savedAt",
  "unknownTitle",
];
const ownsOnly = (value, fields) =>
  isObject(value) && Object.keys(value).every((key) => fields.includes(key));
const timestamp = (value) =>
  Number.isSafeInteger(value) && value >= 0 && value <= 8640000000000000;
const pick = (value, fields) =>
  Object.fromEntries(
    fields
      .filter((key) => Object.hasOwn(value, key))
      .map((key) => [key, value[key]]),
  );
const blank = (resource) =>
  resource === "sources"
    ? []
    : resource === "settings"
      ? { theme: "auto", layout: "grid" }
      : {};
function url(value, optional = false) {
  if (optional && value === "") return true;
  if (!isText(value, 8192) || /[\u0000-\u001f\u007f]/.test(value)) return false;
  try {
    const parsed = new URL(value);
    return (
      ["https:", "http:"].includes(parsed.protocol) &&
      !parsed.username &&
      !parsed.password
    );
  } catch {
    return false;
  }
}
function state(storage) {
  const raw = storage.getItem(STATE_KEY);
  const value = raw ? JSON.parse(raw) : {};
  assert(
    isObject(value) && (value.version === undefined || value.version === 1),
  );
  return value;
}
export function validate(resource, document) {
  assert(resources.includes(resource));
  const data = documentData(document);
  assert(ownsOnly(data, [resource]));
  const value = Object.hasOwn(data, resource)
    ? data[resource]
    : blank(resource);
  if (resource === "sources") {
    assert(Array.isArray(value) && value.length <= 40);
    for (const source of value) {
      assert(ownsOnly(source, sourceFields));
      assert(isText(source.id, 120) && /^[a-zA-Z0-9_-]+$/.test(source.id));
      assert(url(source.url) && isText(source.name, 255));
      assert(
        source.enabled === undefined || typeof source.enabled === "boolean",
      );
      assert(source.updatedAt === undefined || timestamp(source.updatedAt));
    }
    assert(new Set(value.map((source) => source.id)).size === value.length);
    assert(
      new Set(value.map((source) => new URL(source.url).href)).size ===
        value.length,
    );
  } else if (resource === "read") {
    assert(isObject(value) && Object.keys(value).length <= 8000);
    for (const [key, at] of Object.entries(value))
      assert(url(key) && timestamp(at));
  } else if (resource === "saved") {
    assert(isObject(value) && Object.keys(value).length <= 500);
    for (const [key, article] of Object.entries(value)) {
      assert(ownsOnly(article, articleFields));
      assert(url(key) && article.id === key && url(article.link));
      assert(isText(article.title, 4000));
      assert(article.summary === undefined || isText(article.summary, 3000));
      assert(article.image === undefined || url(article.image, true));
      assert(article.sourceId === undefined || isText(article.sourceId, 120));
      assert(
        article.sourceName === undefined || isText(article.sourceName, 255),
      );
      assert(article.savedAt === undefined || timestamp(article.savedAt));
      assert(
        article.publishedAt === undefined ||
          (Number.isSafeInteger(article.publishedAt) &&
            Math.abs(article.publishedAt) <= 8640000000000000),
      );
      assert(
        article.unknownTitle === undefined ||
          typeof article.unknownTitle === "boolean",
      );
    }
  } else {
    assert(isObject(value));
    for (const [key, setting] of Object.entries(value)) {
      if (key === "readingSize")
        assert(Number.isInteger(setting) && setting >= 16 && setting <= 26);
      else
        assert(
          Object.hasOwn(preferenceValues, key) &&
            preferenceValues[key].includes(setting),
        );
    }
  }
  // Keep an actionable error on oversized documents; never silently truncate personal data.
  if (
    new TextEncoder().encode(JSON.stringify({ schemaVersion: 1, data }))
      .length >
    8 * 1024 * 1024
  )
    throw new Error(
      "Diese News-Sync-Datei ist größer als 8 MiB. Deine lokalen Daten bleiben erhalten.",
    );
  return data;
}
export function snapshot(resource, storage = localStorage) {
  assert(resources.includes(resource));
  const local = state(storage);
  let value = local[resource] ?? blank(resource);
  if (resource === "settings")
    value = {
      ...blank(resource),
      ...pick(local.preferences || {}, [
        ...Object.keys(preferenceValues),
        "readingSize",
      ]),
    };
  if (resource === "sources" && Array.isArray(value))
    value = value.map((source) => pick(source, sourceFields));
  if (resource === "saved" && isObject(value))
    value = Object.fromEntries(
      Object.entries(value).map(([id, article]) => [
        id,
        pick(article, articleFields),
      ]),
    );
  const data = { [resource]: value };
  validate(resource, { schemaVersion: 1, data });
  return data;
}
export const backupKey = (resource) =>
  `julianverse.news.sync-backup:${resource}`;
export function backup(resource, storage = localStorage) {
  const previous = snapshot(resource, storage);
  storage.setItem(backupKey(resource), JSON.stringify(previous));
  return previous;
}
export function apply(resource, document, storage = localStorage) {
  const data = structuredClone(validate(resource, document));
  const previous = backup(resource, storage);
  // Read the latest document for each category. An import must preserve all other categories.
  const current = state(storage);
  const value = data[resource] ?? blank(resource);
  if (resource === "settings") {
    const localOnly = Object.fromEntries(
      Object.entries(current.preferences || {}).filter(
        ([key]) =>
          !Object.hasOwn(preferenceValues, key) && key !== "readingSize",
      ),
    );
    current.preferences = { ...localOnly, ...blank(resource), ...value };
  } else current[resource] = value;
  current.version = 1;
  current.updatedAt = Date.now();
  storage.setItem(STATE_KEY, JSON.stringify(current));
  return previous;
}
