import test from "node:test";
import assert from "node:assert/strict";
import { createJSONAdapter } from "../integrations/sdk/sdk.mjs";

function fixture() {
  const items = new Map();
  const storage = {
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value),
    removeItem: (key) => items.delete(key),
  };
  const adapter = createJSONAdapter({
    app: "app-" + "a".repeat(24),
    resources: [
      { key: "notes", label: "Notizen" },
      { key: "settings", label: "Einstellungen" },
    ],
    storage,
  });
  return { adapter, storage };
}
test("local writes need no account and each import preserves other categories", () => {
  const { adapter, storage } = fixture();
  adapter.set("notes", { text: "lokal" });
  adapter.set("settings", { theme: "dark" });
  storage.setItem("unrelated", "keep");
  adapter.apply("notes", {
    schemaVersion: 1,
    data: { value: { text: "cloud" } },
  });
  assert.deepEqual(adapter.get("notes"), { text: "cloud" });
  assert.deepEqual(adapter.get("settings"), { theme: "dark" });
  assert.deepEqual(JSON.parse(storage.getItem(adapter.backupKey("notes"))), {
    value: { text: "lokal" },
  });
  assert.equal(storage.getItem("unrelated"), "keep");
});
test("malformed, foreign and oversized documents fail before any local replacement", () => {
  const { adapter } = fixture();
  adapter.set("notes", ["keep"]);
  for (const document of [
    { schemaVersion: 2, data: { value: [] } },
    { schemaVersion: 1, data: { stolen: [] } },
    { schemaVersion: 1, data: { value: "x".repeat(512 * 1024) } },
  ]) {
    assert.throws(() => adapter.apply("notes", document));
    assert.deepEqual(adapter.get("notes"), ["keep"]);
  }
  assert.throws(() => adapter.set("../settings", {}));
  assert.throws(() => adapter.set("notes", undefined));
  assert.deepEqual(adapter.get("notes"), ["keep"]);
});
test("deletion documents clear only their category and preserve a backup", () => {
  const { adapter } = fixture();
  adapter.set("notes", "old");
  adapter.set("settings", { size: 10 });
  adapter.apply("notes", { schemaVersion: 1, data: {}, deleted: true });
  assert.equal(adapter.get("notes"), null);
  assert.deepEqual(adapter.get("settings"), { size: 10 });
});
