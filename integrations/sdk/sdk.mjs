import { assert, documentData, isObject } from "../browser/data.mjs";
export {
  JulianverseSync,
  SyncError,
  IndexedDBStore,
} from "../../account/static/js/sync.mjs";
export {
  beginLogin,
  finishLogin,
} from "../../account/static/js/oidc-client.mjs";

// Personal values remain in the app's local storage. Nothing is uploaded here.
export function createJSONAdapter({ app, resources, storage = localStorage }) {
  assert(/^app-[a-f0-9]{24}$/.test(app));
  const names = resources.map((resource) => resource.key);
  assert(names.length <= 10 && new Set(names).size === names.length);
  assert(names.every((name) => /^[a-z][a-z0-9-]{0,39}$/.test(name)));
  const keys = Object.fromEntries(
    names.map((name) => [name, [`julianverse-data:${app}:${name}`]]),
  );
  const check = (name) => assert(names.includes(name));
  const adapter = {
    resources: names,
    keys,
    labels: Object.fromEntries(
      ["de", "en"].map((language) => [
        language,
        Object.fromEntries(
          resources.map((resource) => [resource.key, resource.label]),
        ),
      ]),
    ),
    snapshot(name) {
      check(name);
      const raw = storage.getItem(keys[name][0]);
      const data = { value: raw === null ? null : JSON.parse(raw) };
      adapter.validate(name, { schemaVersion: 1, data });
      return data;
    },
    validate(name, document) {
      check(name);
      const data = documentData(document);
      assert(
        isObject(data) && Object.keys(data).every((key) => key === "value"),
      );
      assert(document.deleted || Object.hasOwn(data, "value"));
      if (
        new TextEncoder().encode(JSON.stringify(document)).length >
        512 * 1024
      )
        throw new Error(
          "Diese Datei überschreitet 512 KiB. Deine lokale Kopie bleibt erhalten.",
        );
      return data;
    },
    backupKey(name) {
      check(name);
      return `${keys[name][0]}:backup`;
    },
    backup(name) {
      const previous = adapter.snapshot(name);
      storage.setItem(adapter.backupKey(name), JSON.stringify(previous));
      return previous;
    },
    apply(name, document) {
      const data = adapter.validate(name, document);
      const previous = adapter.backup(name);
      if (document.deleted) storage.removeItem(keys[name][0]);
      else storage.setItem(keys[name][0], JSON.stringify(data.value));
      return previous;
    },
    get(name) {
      return adapter.snapshot(name).value;
    },
    set(name, value) {
      // Serialize before validation so undefined, cycles and unsupported values cannot corrupt storage.
      const serialized = JSON.stringify({ schemaVersion: 1, data: { value } });
      const document = JSON.parse(serialized);
      adapter.validate(name, document);
      storage.setItem(keys[name][0], JSON.stringify(document.data.value));
      if (typeof window !== "undefined")
        window.dispatchEvent(
          new CustomEvent("julianverse:change", {
            detail: { key: keys[name][0] },
          }),
        );
    },
  };
  return adapter;
}

export async function mountAccount({
  root,
  config,
  adapter,
  onApply = () => {},
}) {
  if (!root) throw new Error("Der Account-Bereich fehlt.");
  const { AccountPanel } = await import("../browser/panel.mjs");
  return new AccountPanel({
    root,
    config: { scope: "openid profile sync", ...config },
    adapter,
    onApply,
  });
}
