import { AccountPanel } from "./panel.mjs";
import * as adapter from "./adapter.mjs";
import { config } from "./config.mjs";

if (!window.julianverseReady) {
  await new Promise((resolve) =>
    window.addEventListener("julianverse:ready", resolve, { once: true }),
  );
}

// The same panel is available in the feed overview and on the separate reading page.
const dialog = document.getElementById("news-account-dialog");
document
  .querySelectorAll("[data-open-account]")
  .forEach((button) =>
    button.addEventListener("click", () => dialog.showModal()),
  );
dialog
  .querySelector("[data-close-account]")
  .addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target !== dialog) return;
  const box = dialog.getBoundingClientRect();
  if (
    event.clientX < box.left ||
    event.clientX > box.right ||
    event.clientY < box.top ||
    event.clientY > box.bottom
  )
    dialog.close();
});
window.addEventListener("julianverse:news-change", () =>
  window.dispatchEvent(
    new CustomEvent("julianverse:change", {
      detail: { key: adapter.STATE_KEY },
    }),
  ),
);

new AccountPanel({
  root: document.getElementById("julianverse-account"),
  config: {
    ...config,
    redirectUri: new URL("../account-callback.html", import.meta.url).href,
  },
  adapter,
  onApply: (resource) => window.julianverseApply(resource),
});
