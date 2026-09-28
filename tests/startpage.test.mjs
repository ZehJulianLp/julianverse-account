import test from "node:test";
import assert from "node:assert/strict";
import { snapshot, apply } from "../integrations/startpage/adapter.mjs";

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

test("Startpage exports only the selected category and never API credentials", () => {
  const storage = new Storage();
  storage.setItem("notes", JSON.stringify("private notes"));
  storage.setItem("ai.agent.apiKey", JSON.stringify("private key"));
  storage.setItem("theme", JSON.stringify("dark"));
  assert.deepEqual(snapshot("settings", storage), { theme: "dark" });
  assert.deepEqual(snapshot("notes", storage), { notes: "private notes" });
  assert.throws(() => snapshot("profiles", storage), /noch nicht/);
});

test("Startpage preserves a local backup and rejects writes outside the category", () => {
  const storage = new Storage();
  storage.setItem("notes", JSON.stringify("old notes"));
  assert.throws(
    () =>
      apply("notes", { schemaVersion: 1, data: { theme: "dark" } }, storage),
    /fremde/,
  );
  apply("notes", { schemaVersion: 1, data: { notes: "cloud notes" } }, storage);
  assert.equal(JSON.parse(storage.getItem("notes")), "cloud notes");
  assert.deepEqual(JSON.parse(storage.getItem("julianverse.backup.notes")), {
    notes: "old notes",
  });
});

test("Startpage default bookmark keys survive sync; executable links remain blocked", () => {
  const storage = new Storage();
  const tiles = [{ title: "GitHub", url: "https://github.com", key: "gh" }];
  storage.setItem("tiles", JSON.stringify(tiles));
  const data = snapshot("bookmarks", storage);
  apply("bookmarks", { schemaVersion: 1, data }, storage);
  assert.deepEqual(JSON.parse(storage.getItem("tiles")), tiles);
  for (const url of [
    "javascript:alert(1)",
    "https://user:secret@example.org",
  ]) {
    assert.throws(() =>
      apply(
        "bookmarks",
        { schemaVersion: 1, data: { tiles: [{ ...tiles[0], url }] } },
        storage,
      ),
    );
  }
});
