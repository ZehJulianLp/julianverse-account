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
