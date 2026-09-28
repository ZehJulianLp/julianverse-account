const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
const decode = (value) =>
  Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), (c) =>
    c.charCodeAt(0),
  );
const encode = (buffer) =>
  btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

async function post(path, data) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok)
    throw new Error(
      body.message ||
        "Die Anfrage ist fehlgeschlagen. Lade die Seite neu und versuche es erneut.",
    );
  return body;
}

for (const button of document.querySelectorAll("[data-passkey]")) {
  button.addEventListener("click", async () => {
    const message = button.closest(".card").querySelector(".client-message");
    message.textContent = "";
    button.disabled = true;
    try {
      if (!window.PublicKeyCredential || !window.isSecureContext)
        throw new Error(
          "Passkeys benötigen HTTPS und einen Browser mit Passkey-Unterstützung.",
        );
      const register = button.dataset.passkey === "register";
      const action = register ? "register" : "login";
      const { options, challenge } = await post(
        `/api/passkeys/${action}/options`,
        { next: button.dataset.next },
      );
      options.challenge = decode(options.challenge);
      if (register) {
        options.user.id = decode(options.user.id);
        options.excludeCredentials = options.excludeCredentials?.map(
          (item) => ({ ...item, id: decode(item.id) }),
        );
      } else {
        options.allowCredentials = options.allowCredentials?.map((item) => ({
          ...item,
          id: decode(item.id),
        }));
      }
      const credential = await navigator.credentials[
        register ? "create" : "get"
      ]({ publicKey: options });
      const body = {
        id: credential.id,
        rawId: encode(credential.rawId),
        type: credential.type,
        response: {
          clientDataJSON: encode(credential.response.clientDataJSON),
        },
      };
      if (register) {
        body.response.attestationObject = encode(
          credential.response.attestationObject,
        );
        body.response.transports = credential.response.getTransports?.() || [];
      } else {
        body.response.authenticatorData = encode(
          credential.response.authenticatorData,
        );
        body.response.signature = encode(credential.response.signature);
        body.response.userHandle = credential.response.userHandle
          ? encode(credential.response.userHandle)
          : null;
      }
      const result = await post(`/api/passkeys/${action}/verify`, {
        challenge,
        credential: body,
        name: document.querySelector("#passkey-name")?.value,
      });
      location.assign(result.redirect);
    } catch (error) {
      message.textContent =
        error.name === "NotAllowedError"
          ? "Die Passkey-Anfrage wurde abgebrochen oder ist abgelaufen."
          : error.message;
    } finally {
      button.disabled = false;
    }
  });
}
document
  .querySelector("[data-print]")
  ?.addEventListener("click", () => window.print());
