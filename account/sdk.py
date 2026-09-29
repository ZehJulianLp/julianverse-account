"""Build a static starter ZIP from the same tested modules as the first-party apps."""

import html
import io
import json
import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from flask import abort, current_app, send_file


def starter_zip(item):
    root = Path(__file__).resolve().parents[1]
    path = unquote(urlsplit(item.website).path).lstrip("/")
    if not path or path.endswith("/"):
        path += "index.html"
    elif not posixpath.splitext(path)[1]:
        path += "/index.html"
    directory = posixpath.dirname(path)
    if posixpath.basename(path) in ("app.mjs", "styles.css", "README.md"):
        abort(
            400,
            "Die App-Adresse kollidiert mit einer Datei der Vorlage. Verwende für die App ein eigenes Verzeichnis oder index.html.",
        )
    asset_dir = posixpath.join(directory, "account")
    callback = unquote(urlsplit(item.client.redirect_uris[0]).path).lstrip("/")
    config = {
        "issuer": current_app.config["BASE_URL"],
        "app": item.client.slug,
        "clientId": item.client_id,
        "name": item.client.client_name,
        "redirectUri": item.client.redirect_uris[0],
        "scope": "openid profile sync",
        "resources": [
            dict(key=r.key, label=r.label, description=r.description)
            for r in item.resources
            if r.enabled
        ],
    }
    files = {path: (root / "integrations/sdk/starter/index.html").read_bytes()}
    for name in ("app.mjs", "styles.css"):
        files[posixpath.join(directory, name)] = (
            root / "integrations/sdk/starter" / name
        ).read_bytes()
    for name in ("panel.mjs", "panel.css", "data.mjs", "session.mjs", "callback.mjs"):
        files[f"{asset_dir}/{name}"] = (root / "integrations/browser" / name).read_bytes()
    for name in ("sync.mjs", "oidc-client.mjs"):
        files[f"{asset_dir}/{name}"] = (root / "account/static/js" / name).read_bytes()
    sdk = (root / "integrations/sdk/sdk.mjs").read_text()
    files[f"{asset_dir}/sdk.mjs"] = (
        sdk.replace("../browser/", "./").replace("../../account/static/js/", "./").encode()
    )
    files[f"{asset_dir}/config.mjs"] = (
        "// Public client configuration; no secrets.\nexport const config = "
        + json.dumps(config, ensure_ascii=False, indent=2)
        + ";\n"
    ).encode()
    callback_html = (root / "integrations/browser/account-callback.html").read_text()
    relative_module = posixpath.relpath(
        asset_dir + "/callback.mjs", posixpath.dirname(callback) or "."
    )
    if (
        callback == "README.md"
        or callback in files
        or any(name.startswith(callback + "/") or callback.startswith(name + "/") for name in files)
    ):
        abort(
            400,
            "Die erste Rücksprungadresse kollidiert mit einer Datei der Vorlage. Verwende eine eigene Callback-Seite, zum Beispiel account-callback.html.",
        )
    files[callback] = callback_html.replace(
        'src="account/callback.mjs"', 'src="' + html.escape(relative_module, quote=True) + '"'
    ).encode()
    files["README.md"] = (root / "integrations/sdk/README.md").read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            if not name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError("Unsafe archive path")
            archive.writestr(name, data)
    stream.seek(0)
    return send_file(
        stream,
        mimetype="application/zip",
        download_name=item.client.slug + "-starter.zip",
        as_attachment=True,
        max_age=0,
    )
