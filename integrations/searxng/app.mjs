import { AccountPanel } from "./account/panel.mjs";
import { config } from "./account/config.mjs";
import * as adapter from "./adapter.mjs";

const home =
  location.pathname === "/julianverse/" ||
  location.pathname === "/julianverse/index.html";
const categoryLabels = {
  general: "Allgemein",
  images: "Bilder",
  videos: "Videos",
  news: "Nachrichten",
  map: "Karten",
  music: "Musik",
  it: "IT",
  science: "Wissenschaft",
  files: "Dateien",
  "social media": "Social Media",
};
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
};
let panel;
function notice(message) {
  let status = document.getElementById("search-status");
  if (!status) {
    status = node("p", "", "jv-search-notice");
    status.id = "search-status";
    status.setAttribute("role", "status");
    document.querySelector("main")?.prepend(status);
  }
  if (status) status.textContent = message;
}
function changed(resource) {
  window.dispatchEvent(
    new CustomEvent("julianverse:change", {
      detail: { key: adapter.keys[resource][0] },
    }),
  );
}
function save(resource, data) {
  adapter.write(resource, data);
  changed(resource);
}
function safely(action) {
  try {
    action();
  } catch (error) {
    notice(
      error instanceof RangeError || error.message?.includes("Cloud")
        ? error.message
        : "Die Änderung konnte nicht lokal gespeichert werden. Bitte prüfe den verfügbaren Browserspeicher.",
    );
  }
}
function button(text, action) {
  const element = node("button", text);
  element.type = "button";
  element.addEventListener("click", () => safely(action));
  return element;
}
function prune() {
  const previous = adapter.snapshot("history");
  const next = adapter.pruneHistory(previous);
  if (next.entries.length !== previous.entries.length) {
    save("history", next);
    localStorage.removeItem(adapter.backupKey("history"));
  }
}
function replay(item) {
  adapter.validateSearch(item);
  const form = node("form");
  form.action = "/search";
  form.method = "post";
  const values = {
    q: item.q,
    categories: item.category,
    language: item.language,
    time_range: item.timeRange,
    safesearch: item.safeSearch,
  };
  for (const [name, value] of Object.entries(values)) {
    const input = node("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.append(input);
  }
  document.body.append(form);
  form.submit();
}
function renderList(resource) {
  const list = document.getElementById(
    resource === "history" ? "history-list" : "saved-list",
  );
  if (!list) return;
  const data = adapter.snapshot(resource);
  list.replaceChildren();
  if (!data.entries.length)
    list.append(
      node(
        "li",
        resource === "history"
          ? "Noch kein Verlauf auf diesem Gerät."
          : "Noch keine gespeicherten Suchen. Du kannst auch direkt auf einer Ergebnisseite eine Suche speichern.",
        "jv-empty",
      ),
    );
  for (const item of data.entries) {
    const row = node("li");
    const text = node("div", "", "jv-entry-text");
    text.append(node("strong", item.label || item.q));
    if (item.label) text.append(node("p", item.q));
    const detail =
      item.category
        .split(",")
        .map((value) => categoryLabels[value])
        .join(", ") +
      (resource === "history"
        ? " · " +
          new Date(item.at).toLocaleString("de-DE", {
            dateStyle: "medium",
            timeStyle: "short",
          })
        : "");
    text.append(node("small", detail));
    const actions = node("div", "", "jv-row-actions");
    actions.append(button("Suchen", () => replay(item)));
    if (resource === "history")
      actions.append(
        button("Speichern", () => {
          adapter.addSearch("favorites", {
            ...item,
            id: crypto.randomUUID(),
            at: Date.now(),
          });
          changed("favorites");
          render();
          notice("Suche gespeichert.");
        }),
      );
    if (resource === "favorites")
      actions.append(
        button("Umbenennen", () => {
          const label = prompt(
            "Name der gespeicherten Suche",
            item.label || item.q,
          );
          if (label === null) return;
          const current = adapter.snapshot(resource);
          const target = current.entries.find((value) => value.id === item.id);
          if (target) target.label = label.trim().slice(0, 120);
          save(resource, current);
          render();
        }),
      );
    const remove = button("Entfernen", () => {
      const current = adapter.snapshot(resource);
      current.entries = current.entries.filter((value) => value.id !== item.id);
      save(resource, current);
      localStorage.removeItem(adapter.backupKey(resource));
      render();
    });
    remove.setAttribute("aria-label", `${item.label || item.q} entfernen`);
    actions.append(remove);
    row.append(text, actions);
    list.append(row);
  }
}
function theme() {
  if (!home) return;
  const style = adapter.readCookies(document.cookie).simple_style || "auto";
  document.documentElement.dataset.theme = style;
}
function render() {
  if (!home) return;
  theme();
  renderList("favorites");
  renderList("history");
  document.getElementById("saved-count").textContent =
    `${adapter.snapshot("favorites").entries.length} / 200`;
  const recording = localStorage.getItem(adapter.recordingKey) === "true";
  document.getElementById("history-state").textContent = recording
    ? "Aktiv auf diesem Gerät"
    : "Pausiert";
  document.getElementById("record-history").textContent = recording
    ? "Verlauf pausieren"
    : "Verlauf aktivieren";
  document
    .getElementById("record-history")
    .setAttribute("aria-pressed", String(recording));
  document.getElementById("history-retention").value = String(
    adapter.snapshot("history").retentionDays,
  );
  document.getElementById("clear-history").disabled =
    !adapter.snapshot("history").entries.length;
}
function currentSearch() {
  const form = document.querySelector("form#search");
  if (!form) return null;
  const values = new FormData(form);
  const q = String(values.get("q") || "").trim();
  if (!q || q.length > 400) return null;
  const category = String(
    values.get("categories") ||
      adapter.categories
        .filter((name) => values.has(`category_${name}`))
        .join(",") ||
      "general",
  );
  const item = {
    id: crypto.randomUUID(),
    q,
    category,
    language: String(values.get("language") || "auto"),
    timeRange: String(values.get("time_range") || ""),
    safeSearch: String(values.get("safesearch") || "0"),
    at: Date.now(),
  };
  return adapter.validateSearch(item);
}
function init() {
  // Fail locally before attaching account listeners if browser storage is unavailable.
  const probe = "julianverse-search:probe";
  localStorage.setItem(probe, "1");
  localStorage.removeItem(probe);
  const cookies = adapter.readCookies(document.cookie);
  const settings = { cookies };
  if (JSON.stringify(adapter.snapshot("settings")) !== JSON.stringify(settings))
    save("settings", settings);
  prune();
  if (home) {
    document
      .getElementById("save-search")
      .addEventListener("submit", (event) => {
        event.preventDefault();
        safely(() => {
          const form = event.currentTarget;
          const values = new FormData(form);
          const cookies = adapter.readCookies(document.cookie);
          const item = {
            id: crypto.randomUUID(),
            q: String(values.get("q")).trim(),
            label: String(values.get("label")).trim(),
            category: String(values.get("category")),
            language: cookies.language || "auto",
            safeSearch: cookies.safesearch || "0",
            timeRange: "",
            at: Date.now(),
          };
          adapter.addSearch("favorites", item);
          changed("favorites");
          form.reset();
          render();
          notice("Suche gespeichert.");
        });
      });
    document.getElementById("record-history").addEventListener("click", () =>
      safely(() => {
        const recording = localStorage.getItem(adapter.recordingKey) !== "true";
        localStorage.setItem(adapter.recordingKey, String(recording));
        render();
        notice(
          recording
            ? "Neue Suchen werden auf diesem Gerät im Verlauf gespeichert."
            : "Verlauf pausiert. Neue Suchen werden nicht gespeichert.",
        );
      }),
    );
    document
      .getElementById("history-retention")
      .addEventListener("change", (event) =>
        safely(() => {
          const data = adapter.snapshot("history");
          data.retentionDays = Number(event.target.value);
          save("history", adapter.pruneHistory(data));
          localStorage.removeItem(adapter.backupKey("history"));
          render();
        }),
      );
    document.getElementById("clear-history").addEventListener("click", () =>
      safely(() => {
        if (
          !confirm(
            "Den gesamten Suchverlauf löschen? Bei aktivem Verlauf-Sync wird die Löschung beim nächsten erfolgreichen Abgleich in ownCloud übernommen.",
          )
        )
          return;
        save("history", { ...adapter.snapshot("history"), entries: [] });
        localStorage.removeItem(adapter.backupKey("history"));
        render();
        notice(
          "Verlauf lokal gelöscht. Aktiver Sync übernimmt die Löschung beim nächsten erfolgreichen Abgleich.",
        );
      }),
    );
    render();
  } else {
    const link = node("a", "Meine Suche", "jv-search-link");
    link.href = "/julianverse/";
    const navigation = document.getElementById("links_on_top");
    if (navigation) navigation.prepend(link);
    else {
      const nav = node("nav", "", "jv-search-navigation");
      nav.append(link);
      document.querySelector("main")?.prepend(nav);
    }
    const endpoint = document.querySelector('meta[name="endpoint"]')?.content;
    if (endpoint === "results") {
      const item = currentSearch();
      if (item) {
        const toolbar = node("div", "", "jv-search-toolbar");
        toolbar.append(
          button("Suche speichern", () => {
            adapter.addSearch("favorites", item);
            changed("favorites");
            notice("Suche gespeichert. Du findest sie unter „Meine Suche“.");
          }),
        );
        document.querySelector("form#search")?.after(toolbar);
        const navigationType =
          performance.getEntriesByType("navigation")[0]?.type;
        if (
          localStorage.getItem(adapter.recordingKey) === "true" &&
          !["reload", "back_forward"].includes(navigationType) &&
          Number(new URLSearchParams(location.search).get("pageno") || 1) === 1
        ) {
          adapter.addSearch("history", item);
          changed("history");
        }
      }
    }
  }
  let root = document.getElementById("julianverse-account");
  if (!root) {
    root = node("div");
    root.id = "julianverse-account";
    root.hidden = true;
    document.body.append(root);
  }
  panel = new AccountPanel({
    root,
    adapter,
    config: {
      ...config,
      redirectUri: new URL(
        "/julianverse/account-callback.html",
        location.origin,
      ).href,
    },
    onApply: (resource) => {
      if (resource === "settings") {
        for (const cookie of adapter.cookieAssignments(
          adapter.snapshot("settings").cookies,
        ))
          document.cookie = cookie;
        notice(
          "Sucheinstellungen übernommen. Sie gelten ab dem nächsten Seitenaufruf.",
        );
      }
      if (resource === "history")
        setTimeout(
          () =>
            safely(() => {
              prune();
              render();
            }),
          0,
        );
      render();
    },
  });
  window.addEventListener("storage", () =>
    safely(() => {
      prune();
      render();
    }),
  );
  setInterval(() => {
    if (!document.hidden)
      safely(() => {
        prune();
        render();
      });
  }, 60000);
  // Surface failed background sync on search pages without inserting the full panel.
  if (!home)
    new MutationObserver(() => {
      const needsAttention = Object.values(panel.states).some(
        (state) =>
          state.conflict ||
          (state.message &&
            !state.message.startsWith("Abgeglichen") &&
            !state.busy),
      );
      const link = document.querySelector(".jv-search-link");
      if (link) {
        link.textContent = needsAttention
          ? "Meine Suche · Sync prüfen"
          : "Meine Suche";
      }
    }).observe(root, { childList: true });
}
safely(init);
