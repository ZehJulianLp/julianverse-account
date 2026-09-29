import { config } from "./account/config.mjs";
import { createJSONAdapter, mountAccount } from "./account/sdk.mjs";

document.title = config.name;
document.getElementById("app-name").textContent = config.name;
const adapter = createJSONAdapter(config);
const editors = new Map();
const message = document.getElementById("message");
function render(resource) {
  const editor = editors.get(resource);
  if (editor) editor.value = JSON.stringify(adapter.get(resource), null, 2);
}
for (const resource of config.resources) {
  const form = document.createElement("form");
  const label = document.createElement("label");
  label.textContent = resource.label;
  const editor = document.createElement("textarea");
  editor.rows = 8;
  editor.spellcheck = false;
  editor.required = true;
  label.append(editor);
  const help = document.createElement("p");
  help.textContent = resource.description;
  const button = document.createElement("button");
  button.textContent = "Lokal speichern";
  form.append(label, help, button);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      adapter.set(resource.key, JSON.parse(editor.value));
      message.textContent = `${resource.label}: lokal gespeichert.`;
    } catch (error) {
      message.textContent = error.message;
    }
  });
  document.getElementById("editors").append(form);
  editors.set(resource.key, editor);
  render(resource.key);
}
if (!config.resources.length)
  message.textContent =
    "Lege unter „Meine Apps“ Datenarten an und lade die Vorlage erneut herunter.";
window.addEventListener("storage", (event) => {
  for (const resource of adapter.resources)
    if (event.key === adapter.keys[resource][0] || event.key === null)
      render(resource);
});
mountAccount({
  root: document.getElementById("account"),
  config,
  adapter,
  onApply: render,
}).catch((error) => {
  message.textContent = error.message;
});
