import test from "node:test";
import assert from "node:assert/strict";
import * as adapter from "../integrations/searxng/adapter.mjs";

const storage = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
};
const search = (changes = {}) => ({
  id: "test-search",
  q: "Hannover",
  category: "general",
  language: "de-DE",
  safeSearch: "1",
  timeRange: "",
  at: 1780000000000,
  ...changes,
});
test("search cookie roundtrip preserves upstream encoding and excludes credentials", () => {
  const cookies = adapter.readCookies(
    'tokens=private-engine-token; session=private; simple_style=dark; disabled_engines="brave__general\\054google cse__general"; language=de-DE',
  );
  assert.deepEqual(cookies, {
    simple_style: "dark",
    language: "de-DE",
    disabled_engines: "brave__general,google cse__general",
  });
  const assignments = adapter.cookieAssignments(cookies);
  const roundtrip = adapter.readCookies(
    assignments
      .filter((cookie) => !cookie.includes("Max-Age=0;"))
      .map((cookie) => cookie.split(";")[0])
      .join("; "),
  );
  assert.deepEqual(roundtrip, cookies);
  assert(
    !assignments.some(
      (cookie) => cookie.startsWith("tokens=") || cookie.startsWith("session="),
    ),
  );
  assert(
    assignments.every((cookie) => cookie.includes("Secure; SameSite=Lax")),
  );
  assert.deepEqual(
    adapter.readCookies("language=de-DE; simple_style=dark"),
    adapter.readCookies("simple_style=dark; language=de-DE"),
  );
});
test("cloud settings cannot import auth cookies or cookie attributes", () => {
  for (const cookies of [
    { session: "secret" },
    { tokens: "secret" },
    { preferences: "encoded-secret" },
    { language: "de-DE; Domain=.julianverse.de" },
    { simple_style: "dark\r\nSet-Cookie: other=1" },
  ]) {
    assert.throws(() =>
      adapter.validate("settings", { schemaVersion: 1, data: { cookies } }),
    );
  }
});
test("history expiration, limit and same-search deduplication", () => {
  const now = 1780000000000;
  const store = storage();
  adapter.write(
    "history",
    {
      retentionDays: 7,
      entries: [search({ id: "old", at: now - 8 * 86400000 })],
    },
    store,
  );
  adapter.addSearch("history", search(), store, now);
  adapter.addSearch("history", search({ id: "repeated" }), store, now);
  assert.equal(adapter.snapshot("history", store).entries.length, 1);
  assert.equal(adapter.snapshot("history", store).entries[0].id, "repeated");
  for (let i = 0; i < 210; i++)
    adapter.addSearch(
      "history",
      search({ id: `search-${i}`, q: `Query ${i}` }),
      store,
      now,
    );
  assert.equal(adapter.snapshot("history", store).entries.length, 200);
  assert.equal(
    adapter.pruneHistory(adapter.snapshot("history", store), now + 8 * 86400000)
      .entries.length,
    0,
  );
  assert.equal(
    adapter.pruneHistory(
      { retentionDays: 0, entries: [search()] },
      now + 400 * 86400000,
    ).entries.length,
    1,
  );
});
test("full favorites never silently discard an existing search", () => {
  const store = storage();
  adapter.write(
    "favorites",
    {
      entries: Array.from({ length: 200 }, (_, i) =>
        search({ id: `saved-${i}`, q: `Saved ${i}` }),
      ),
    },
    store,
  );
  const before = adapter.snapshot("favorites", store);
  assert.throws(
    () => adapter.addSearch("favorites", search(), store),
    /200 Suchen/,
  );
  assert.deepEqual(adapter.snapshot("favorites", store), before);
  adapter.addSearch("favorites", search({ q: "Saved 0" }), store);
  assert.equal(adapter.snapshot("favorites", store).entries.length, 200);
});
test("imports never enable recording, validate before mutation, and preserve a backup", () => {
  const store = storage();
  adapter.addSearch("favorites", search(), store);
  const original = adapter.snapshot("favorites", store);
  assert.throws(() =>
    adapter.apply(
      "favorites",
      {
        schemaVersion: 1,
        data: { entries: [search({ category: "javascript:alert(1)" })] },
      },
      store,
    ),
  );
  assert.deepEqual(adapter.snapshot("favorites", store), original);
  adapter.apply(
    "history",
    { schemaVersion: 1, data: { retentionDays: 30, entries: [search()] } },
    store,
  );
  assert.equal(store.getItem(adapter.recordingKey), null);
  adapter.apply(
    "favorites",
    { schemaVersion: 1, deleted: true, data: {} },
    store,
  );
  assert.deepEqual(adapter.snapshot("favorites", store), { entries: [] });
  assert.deepEqual(
    JSON.parse(store.getItem(adapter.backupKey("favorites"))),
    original,
  );
});
