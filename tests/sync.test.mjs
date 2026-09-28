import test from "node:test";
import assert from "node:assert/strict";
import { JulianverseSync } from "../account/static/js/sync.mjs";

class MemoryStore {
  rows = new Map();
  async get(key) {
    return structuredClone(this.rows.get(key) || null);
  }
  async set(key, value) {
    this.rows.set(key, structuredClone(value));
  }
}

function environment() {
  const store = new MemoryStore();
  const env = {
    subject: "user-one",
    remote: null,
    etag: null,
    puts: 0,
    requests: 0,
    offline: false,
  };
  const fetcher = async (url, options = {}) => {
    env.requests++;
    if (env.offline) throw new TypeError("offline");
    if (url.endsWith("/userinfo")) return Response.json({ sub: env.subject });
    if (url.endsWith("/startpage"))
      return Response.json({ resources: { notes: true } });
    if (options.method === "PUT") {
      env.puts++;
      if (
        options.headers["If-Match"] !== env.etag &&
        !(options.headers["If-None-Match"] === "*" && !env.etag)
      )
        return Response.json({ error: "conflict" }, { status: 412 });
      env.remote = JSON.parse(options.body);
      env.etag = '"written"';
      return Response.json({ saved: true }, { headers: { ETag: env.etag } });
    }
    return env.remote
      ? Response.json(env.remote, { headers: { ETag: env.etag } })
      : Response.json({ error: "not_found" }, { status: 404 });
  };
  const sdk = new JulianverseSync({
    issuer: "https://account.test",
    app: "startpage",
    store,
    fetcher,
  });
  return { sdk, store, env };
}

test("login does not enable sync or upload local content", async () => {
  const { sdk, env } = environment();
  await sdk.attach("token");
  assert.equal(env.requests, 1);
  await assert.rejects(
    sdk.save("notes", "private note"),
    /nicht eingeschaltet/,
  );
  await assert.rejects(sdk.enable("notes"), /ausdrücklich/);
  assert.equal(env.puts, 0);
});

test("offline edits persist and upload conditionally after reconnection", async () => {
  const { sdk, env } = environment();
  await sdk.attach("token");
  await sdk.enable("notes", { source: "local", localData: "first" });
  env.offline = true;
  await sdk.save("notes", "offline edit");
  await assert.rejects(sdk.sync("notes"), /offline/);
  assert.equal((await sdk.read("notes")).document.data, "offline edit");
  assert.equal((await sdk.read("notes")).dirty, true);
  env.offline = false;
  await sdk.sync("notes");
  assert.equal(env.remote.data, "offline edit");
  assert.equal((await sdk.read("notes")).dirty, false);
});

test("concurrent cloud changes preserve both versions until explicit resolution", async () => {
  const { sdk, env } = environment();
  env.remote = { schemaVersion: 1, data: "original", deleted: false };
  env.etag = '"v1"';
  await sdk.attach("token");
  await sdk.enable("notes", { source: "cloud" });
  await sdk.save("notes", "local");
  env.remote.data = "other device";
  env.etag = '"v2"';
  const conflict = await sdk.sync("notes");
  assert.equal(conflict.document.data, "local");
  assert.equal(conflict.conflict.document.data, "other device");
  assert.equal(env.puts, 0);
  await sdk.resolve("notes", "local");
  await sdk.sync("notes");
  assert.equal(env.remote.data, "local");
  assert.equal((await sdk.read("notes")).previous.data, "other device");
});

test("account switching never imports another user working copy", async () => {
  const { sdk, store, env } = environment();
  await sdk.attach("token");
  await sdk.enable("notes", { source: "local", localData: "user one secret" });
  sdk.disconnect();
  assert.equal(store.rows.size, 1);
  env.subject = "user-two";
  await sdk.attach("different-token");
  assert.equal(await sdk.read("notes"), null);
  await assert.rejects(
    sdk.enable("notes", { source: "resume" }),
    /keinen lokalen/,
  );
  assert.equal(env.puts, 0);
});

test("deletions persist as tombstones and disable stops network requests", async () => {
  const { sdk, env } = environment();
  await sdk.attach("token");
  await sdk.enable("notes", { source: "local", localData: "delete me" });
  await sdk.save("notes", null, { deleted: true });
  await sdk.sync("notes");
  assert.equal(env.remote.deleted, true);
  sdk.stop("notes");
  const before = env.requests;
  await assert.rejects(sdk.sync("notes"), /ausgeschaltet/);
  assert.equal(env.requests, before);
  assert.equal((await sdk.read("notes")).document.deleted, true);
});

test("local editing during a download keeps the original ETag and detects the cloud conflict", async () => {
  const { sdk, env } = environment();
  env.remote = { schemaVersion: 1, data: "original", deleted: false };
  env.etag = '"original"';
  await sdk.attach("token");
  await sdk.enable("notes", { source: "cloud" });
  env.remote = { schemaVersion: 1, data: "other device", deleted: false };
  env.etag = '"other"';
  const record = await sdk.sync("notes", {
    localData: "original",
    readLocal: () => "edited during GET",
  });
  assert.equal(record.document.data, "edited during GET");
  assert.equal(record.etag, '"original"');
  assert.equal(record.conflict.document.data, "other device");
  assert.equal(env.puts, 0);
});

test("JSON key order does not cause another upload or a conflict", async () => {
  const { sdk, env } = environment();
  const local = { theme: "dark", charts: { wind: true, rain: false } };
  env.remote = {
    data: { charts: { rain: false, wind: true }, theme: "dark" },
    deleted: false,
    schemaVersion: 1,
  };
  env.etag = '"same-file"';
  await sdk.attach("token");
  await sdk.enable("notes", { source: "local", localData: local });
  const record = await sdk.sync("notes");
  assert.equal(record.dirty, false);
  assert.equal(record.conflict, null);
  assert.equal(env.puts, 0);
});

for (const savedEtag of [
  '"0123456789abcdef0123456789abcdef"',
  '"0123456789abcdef0123456789abcdef-gzip"',
]) {
  test(`legacy compression conflict is repaired safely from ${savedEtag}`, async () => {
    const { sdk, store, env } = environment();
    const base = '"0123456789abcdef0123456789abcdef"';
    env.remote = { schemaVersion: 1, data: { theme: "light" }, deleted: false };
    env.etag = base;
    await sdk.attach("token");
    await sdk.enable("notes", { source: "cloud" });
    await sdk.save("notes", { theme: "dark" });
    const record = await sdk.read("notes");
    record.etag = savedEtag;
    record.conflict = {
      document: structuredClone(env.remote),
      etag: base.slice(0, -1) + '-gzip"',
    };
    await store.set(sdk.context("notes").key, record);
    const repaired = await sdk.sync("notes");
    assert.equal(repaired.conflict, null);
    assert.equal(repaired.dirty, false);
    assert.equal(env.puts, 1);
    assert.deepEqual(env.remote.data, { theme: "dark" });
  });
}

test("old gzip versions never hide a genuine remote edit", async () => {
  const { sdk, store, env } = environment();
  env.remote = { schemaVersion: 1, data: "original", deleted: false };
  env.etag = '"0123456789abcdef0123456789abcdef-gzip"';
  await sdk.attach("token");
  await sdk.enable("notes", { source: "cloud" });
  await sdk.save("notes", "local edit");
  const record = await sdk.read("notes");
  env.remote = { schemaVersion: 1, data: "cloud edit", deleted: false };
  env.etag = '"fedcba9876543210fedcba9876543210"';
  record.conflict = {
    document: structuredClone(env.remote),
    etag: env.etag.slice(0, -1) + '-gzip"',
  };
  await store.set(sdk.context("notes").key, record);
  const result = await sdk.sync("notes");
  assert.ok(result.conflict);
  assert.equal(result.document.data, "local edit");
  assert.equal(env.puts, 0);
});

test("stored conflicts clear if both copies are already identical; array order still matters", async () => {
  const { sdk, env } = environment();
  env.remote = {
    schemaVersion: 1,
    data: { places: ["A", "B"] },
    deleted: false,
  };
  env.etag = '"original"';
  await sdk.attach("token");
  await sdk.enable("notes", { source: "cloud" });
  await sdk.save("notes", { places: ["B", "A"] });
  env.etag = '"newer"';
  assert.ok((await sdk.sync("notes")).conflict);
  env.remote = {
    deleted: false,
    data: { places: ["B", "A"] },
    schemaVersion: 1,
  };
  const record = await sdk.sync("notes");
  assert.equal(record.conflict, null);
  assert.equal(record.dirty, false);
  assert.equal(record.etag, env.etag);
  assert.equal(env.puts, 0);
});
