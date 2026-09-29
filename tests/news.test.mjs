import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import * as news from "../integrations/news/adapter.mjs";

class Storage {
  data = new Map();
  getItem(key) {
    return this.data.get(key) ?? null;
  }
  setItem(key, value) {
    this.data.set(key, value);
  }
}
const doc = (data) => ({ schemaVersion: 1, data });
const url = "https://example.org/article";
const original = () => ({
  version: 1,
  updatedAt: 42,
  sources: [
    {
      id: "feed-one",
      name: "Example",
      url: "https://example.org/feed",
      enabled: true,
      updatedAt: 21,
    },
  ],
  saved: {
    [url]: {
      id: url,
      link: url,
      title: "Saved article",
      summary: "Preview",
      publishedAt: 0,
      savedAt: 42,
      feedContent: "<p>Full article stays local</p>",
    },
  },
  read: { [url]: 42 },
  preferences: {
    theme: "light",
    layout: "grid",
    readingSize: 20,
    deviceOnly: "local",
  },
});
function store() {
  const storage = new Storage();
  storage.setItem(news.STATE_KEY, JSON.stringify(original()));
  storage.setItem("julianverse.news.cache.v1", "private-feed-cache");
  storage.setItem("julianverse.news.articles.v1", "private-article-html");
  storage.setItem("theme", '"dark"');
  return storage;
}
test("News exports only the selected category and excludes HTML, caches and update noise", () => {
  const storage = store();
  const saved = news.snapshot("saved", storage);
  assert.equal(saved.saved[url].summary, "Preview");
  assert(!("feedContent" in saved.saved[url]));
  const settings = news.snapshot("settings", storage);
  assert.deepEqual(settings, {
    settings: { theme: "light", layout: "grid", readingSize: 20 },
  });
  const state = JSON.parse(storage.getItem(news.STATE_KEY));
  state.updatedAt++;
  storage.setItem(news.STATE_KEY, JSON.stringify(state));
  assert.deepEqual(news.snapshot("settings", storage), settings);
  assert.deepEqual(news.snapshot("read", storage), { read: { [url]: 42 } });
});
test("partial imports preserve unrelated data, unknown local preferences and backups", () => {
  const storage = store();
  const before = news.snapshot("sources", storage);
  news.apply("sources", doc({ sources: [] }), storage);
  assert.deepEqual(news.snapshot("sources", storage), { sources: [] });
  assert.deepEqual(
    JSON.parse(storage.getItem(news.backupKey("sources"))),
    before,
  );
  news.apply(
    "settings",
    doc({ settings: { theme: "dark", readingFont: "serif" } }),
    storage,
  );
  const state = JSON.parse(storage.getItem(news.STATE_KEY));
  assert.deepEqual(state.saved, original().saved);
  assert.deepEqual(state.read, original().read);
  assert.equal(state.preferences.deviceOnly, "local");
  assert.equal(state.preferences.readingSize, undefined);
  assert.equal(state.preferences.layout, "grid");
  assert.equal(
    storage.getItem("julianverse.news.cache.v1"),
    "private-feed-cache",
  );
  assert.equal(
    storage.getItem("julianverse.news.articles.v1"),
    "private-article-html",
  );
  assert.equal(storage.getItem("theme"), '"dark"');
});
test("invalid URLs, duplicate sources and foreign categories never modify News data", () => {
  const storage = store();
  const before = storage.getItem(news.STATE_KEY);
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,x",
    "https://name:secret@example.org/feed",
  ]) {
    assert.throws(() =>
      news.apply(
        "sources",
        doc({ sources: [{ ...original().sources[0], url }] }),
        storage,
      ),
    );
    assert.throws(() =>
      news.apply("read", doc({ read: { [url]: 1 } }), storage),
    );
    assert.throws(() =>
      news.apply(
        "saved",
        doc({ saved: { [url]: { id: url, link: url, title: "bad" } } }),
        storage,
      ),
    );
  }
  assert.throws(() =>
    news.apply(
      "sources",
      doc({ sources: [...original().sources, ...original().sources] }),
      storage,
    ),
  );
  assert.throws(() =>
    news.apply("read", doc({ settings: { theme: "dark" } }), storage),
  );
  assert.throws(() =>
    news.apply(
      "settings",
      doc({ settings: { readingSize: "19px; display:none" } }),
      storage,
    ),
  );
  assert.throws(() =>
    news.apply("saved", doc({ saved: original().saved }), storage),
  );
  assert.equal(storage.getItem(news.STATE_KEY), before);
  assert.equal(storage.getItem(news.backupKey("sources")), null);
});
test("category deletions preserve the other categories and repeated imports are stable", () => {
  const storage = store();
  news.apply("read", { schemaVersion: 1, deleted: true, data: {} }, storage);
  assert.deepEqual(news.snapshot("read", storage), { read: {} });
  const sources = news.snapshot("sources", storage);
  news.apply("sources", doc(sources), storage);
  assert.deepEqual(news.snapshot("sources", storage), sources);
  assert.deepEqual(
    news.snapshot("saved", storage).saved[url].title,
    "Saved article",
  );
});
test("News supports its existing 8000 read markers and 500 saved previews without truncation", () => {
  const read = Object.fromEntries(
    Array.from({ length: 8000 }, (_, i) => [
      `https://example.org/${i}/${"article-".repeat(16)}`,
      i,
    ]),
  );
  const saved = Object.fromEntries(
    Array.from({ length: 500 }, (_, i) => {
      const id = `https://example.org/${i}`;
      return [
        id,
        {
          id,
          link: id,
          title: "Title",
          summary: "Vorschau ".repeat(300),
          savedAt: i,
        },
      ];
    }),
  );
  assert.equal(
    Object.keys(news.validate("read", doc({ read })).read).length,
    8000,
  );
  assert.equal(
    Object.keys(news.validate("saved", doc({ saved })).saved).length,
    500,
  );
});
test(
  "News worker bypasses OAuth callbacks and includes the complete integration offline",
  { skip: !process.env.JULIANVERSE_NEWS_CHECKOUT },
  () => {
    const source = fs.readFileSync(
      `${process.env.JULIANVERSE_NEWS_CHECKOUT}/sw.js`,
      "utf8",
    );
    const handlers = {};
    const context = {
      URL,
      Request,
      self: {
        location: { href: "https://julianverse.de/news/sw.js" },
        addEventListener: (event, handler) => (handlers[event] = handler),
      },
    };
    vm.runInNewContext(source, context);
    for (const path of [
      "account-callback.html?code=secret&state=nonce",
      "?code=secret&state=nonce",
    ]) {
      handlers.fetch({
        request: {
          method: "GET",
          mode: "navigate",
          url: `https://julianverse.de/news/${path}`,
        },
        respondWith: () => assert.fail("OAuth response must bypass caches"),
      });
    }
    assert(
      source.includes("./account/app.mjs") &&
        source.includes("./account/adapter.mjs") &&
        source.includes("./account/config.mjs"),
    );
    assert(!/FILES[^;]*account-callback\.html/.test(source));
  },
);
