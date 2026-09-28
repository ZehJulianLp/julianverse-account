import { assert, isObject, isText, documentData } from "../browser/data.mjs";

const prefix = "julianverse-search:";
export const keys = Object.fromEntries(
  ["favorites", "history", "settings"].map((name) => [name, [prefix + name]]),
);
export const resources = Object.keys(keys);
export const labels = {
  de: {
    favorites: "Gespeicherte Suchen",
    history: "Suchverlauf",
    settings: "Sucheinstellungen",
  },
  en: {
    favorites: "Saved searches",
    history: "Search history",
    settings: "Search preferences",
  },
};
export const recordingKey = prefix + "recording";
export const retentionOptions = [7, 30, 90, 0];
export const categories = [
  "general",
  "images",
  "videos",
  "news",
  "map",
  "music",
  "it",
  "science",
  "files",
  "social media",
];
const booleans = [
  "image_proxy",
  "results_on_new_tab",
  "center_alignment",
  "query_in_title",
  "search_on_category_select",
];
const enums = {
  method: ["GET", "POST"],
  safesearch: ["0", "1", "2"],
  theme: ["simple"],
  simple_style: ["", "auto", "light", "dark", "black"],
  hotkeys: ["default", "vim"],
  url_formatting: ["pretty", "full", "host"],
};
// Never read or import arbitrary cookies, engine access tokens, or encoded preference URLs.
export const cookieNames = [
  ...booleans,
  ...Object.keys(enums),
  "language",
  "locale",
  "autocomplete",
  "favicon_resolver",
  "doi_resolver",
  "categories",
  "disabled_engines",
  "enabled_engines",
  "disabled_plugins",
  "enabled_plugins",
];
const empty = (resource) =>
  resource === "settings"
    ? { cookies: {} }
    : resource === "history"
      ? { entries: [], retentionDays: 30 }
      : { entries: [] };
const exactKeys = (object, names) =>
  isObject(object) && Object.keys(object).every((name) => names.includes(name));
export function validateSearch(item) {
  assert(
    exactKeys(item, [
      "id",
      "q",
      "category",
      "language",
      "timeRange",
      "safeSearch",
      "at",
      "label",
    ]),
  );
  assert(isText(item.id, 80) && /^[a-zA-Z0-9_-]+$/.test(item.id));
  assert(isText(item.q, 400) && item.q.trim().length > 0);
  assert(!/[\u0000-\u001f\u007f]/.test(item.q));
  assert(
    isText(item.category, 150) &&
      item.category.split(",").every((value) => categories.includes(value)),
  );
  assert(isText(item.language, 20) && /^[a-zA-Z_-]*$/.test(item.language));
  assert(["", "day", "week", "month", "year"].includes(item.timeRange));
  assert(["0", "1", "2"].includes(item.safeSearch));
  assert(
    Number.isSafeInteger(item.at) &&
      item.at >= 0 &&
      item.at <= 8640000000000000,
  );
  assert(
    item.label === undefined ||
      (isText(item.label, 120) && !/[\u0000-\u001f]/.test(item.label)),
  );
  return item;
}
export function validateCookies(cookies) {
  assert(isObject(cookies));
  for (const [name, value] of Object.entries(cookies)) {
    assert(cookieNames.includes(name) && isText(value, 3500));
    assert(/^[\x20-\x7e]*$/.test(value) && !/[;"\\]/.test(value));
    if (booleans.includes(name)) assert(["0", "1"].includes(value));
    if (Object.hasOwn(enums, name)) assert(enums[name].includes(value));
    if (["language", "locale"].includes(name))
      assert(value.length <= 20 && /^[a-zA-Z_-]*$/.test(value));
    if (name === "categories")
      assert(
        value.split(",").every((v) => [...categories, "none", ""].includes(v)),
      );
  }
  return cookies;
}
export function validate(resource, document) {
  assert(resources.includes(resource));
  const original = documentData(document);
  const data = Object.keys(original).length ? original : empty(resource);
  if (resource === "settings") {
    assert(exactKeys(data, ["cookies"]));
    validateCookies(data.cookies);
  } else {
    assert(
      exactKeys(
        data,
        resource === "history" ? ["entries", "retentionDays"] : ["entries"],
      ),
    );
    assert(Array.isArray(data.entries) && data.entries.length <= 200);
    data.entries.forEach(validateSearch);
    assert(
      new Set(data.entries.map((item) => item.id)).size === data.entries.length,
    );
    if (resource === "history")
      assert(retentionOptions.includes(data.retentionDays));
  }
  return data;
}
export function snapshot(resource, storage = localStorage) {
  const value = storage.getItem(keys[resource]?.[0]);
  return validate(resource, {
    schemaVersion: 1,
    data: value ? JSON.parse(value) : empty(resource),
  });
}
export function write(resource, data, storage = localStorage) {
  validate(resource, { schemaVersion: 1, data });
  storage.setItem(keys[resource][0], JSON.stringify(data));
}
export const backupKey = (resource) => prefix + "backup:" + resource;
export function backup(resource, storage = localStorage) {
  const previous = snapshot(resource, storage);
  storage.setItem(backupKey(resource), JSON.stringify(previous));
  return previous;
}
export function apply(resource, document, storage = localStorage) {
  const data = structuredClone(validate(resource, document));
  const previous = backup(resource, storage);
  write(resource, data, storage);
  return previous;
}
export function pruneHistory(data, now = Date.now()) {
  return {
    ...data,
    entries: data.entries
      .filter(
        (item) =>
          !data.retentionDays || item.at > now - data.retentionDays * 86400000,
      )
      .slice(0, 200),
  };
}
export function searchIdentity(item) {
  return JSON.stringify([
    item.q,
    item.category,
    item.language,
    item.timeRange,
    item.safeSearch,
  ]);
}
export function addSearch(
  resource,
  item,
  storage = localStorage,
  now = Date.now(),
) {
  validateSearch(item);
  assert(["history", "favorites"].includes(resource));
  let data = snapshot(resource, storage);
  if (resource === "history") data = pruneHistory(data, now);
  if (
    resource === "favorites" &&
    data.entries.length >= 200 &&
    !data.entries.some((old) => searchIdentity(old) === searchIdentity(item))
  ) {
    throw new RangeError(
      "Es sind bereits 200 Suchen gespeichert. Entferne zuerst einen Eintrag.",
    );
  }
  data.entries = [
    item,
    ...data.entries.filter(
      (old) => searchIdentity(old) !== searchIdentity(item),
    ),
  ].slice(0, 200);
  write(resource, data, storage);
}
export function readCookies(raw) {
  const cookies = {};
  for (const part of raw.split(/;\s*/)) {
    const equal = part.indexOf("=");
    const name = part.slice(0, equal).trim();
    if (equal < 0 || !cookieNames.includes(name)) continue;
    let value = part.slice(equal + 1);
    // Werkzeug quotes commas/spaces and uses octal escapes. Percent signs stay literal.
    if (value.startsWith('"') && value.endsWith('"'))
      value = value
        .slice(1, -1)
        .replace(/\\([0-3][0-7]{2}|.)/g, (_, part) =>
          /^[0-3][0-7]{2}$/.test(part)
            ? String.fromCharCode(parseInt(part, 8))
            : part,
        );
    validateCookies({ [name]: value });
    cookies[name] = value;
  }
  return Object.fromEntries(
    cookieNames
      .filter((name) => Object.hasOwn(cookies, name))
      .map((name) => [name, cookies[name]]),
  );
}
export function cookieAssignments(cookies) {
  validateCookies(cookies);
  return cookieNames.map((name) => {
    const exists = Object.hasOwn(cookies, name);
    let value = cookies[name] || "";
    if (/[,\s]/.test(value)) value = '"' + value.replace(/,/g, "\\054") + '"';
    return `${name}=${value}; Path=/; Max-Age=${exists ? 31536000 : 0}; Secure; SameSite=Lax`;
  });
}
