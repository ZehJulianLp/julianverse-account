#!/usr/bin/env python3
"""Build public SearXNG assets; optionally register the public PKCE client."""

import argparse
import json
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLBACK = "https://search.julianverse.de/julianverse/account-callback.html"


def register_client():
    sys.path.insert(0, str(ROOT))
    from account import create_app
    from account.extensions import db
    from account.models import Client, now

    with create_app().app_context():
        client = db.session.scalar(db.select(Client).where(Client.slug == "searxng"))
        if client:
            if (
                not client.enabled
                or client.token_endpoint_auth_method != "none"
                or client.redirect_uris != [CALLBACK]
                or set(client.scope.split()) != {"openid", "profile", "sync"}
            ):
                raise RuntimeError("Der vorhandene SearXNG-Client hat eine andere Konfiguration.")
        else:
            client = Client(
                slug="searxng",
                client_id=secrets.token_urlsafe(24),
                client_secret="",
                client_id_issued_at=now(),
            )
            client.set_client_metadata(
                dict(
                    client_name="Julianverse Search",
                    redirect_uris=[CALLBACK],
                    scope="openid profile sync",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                )
            )
            db.session.add(client)
            db.session.commit()
        return client.client_id


def build(target, client_id, issuer="https://account.julianverse.de"):
    target.mkdir(parents=True, exist_ok=True)
    account = target / "account"
    account.mkdir(exist_ok=True)
    for name in ("index.html", "search.css", "app.mjs"):
        shutil.copyfile(ROOT / "integrations/searxng" / name, target / name)
    adapter = (ROOT / "integrations/searxng/adapter.mjs").read_text()
    (target / "adapter.mjs").write_text(
        adapter.replace("../browser/data.mjs", "./account/data.mjs")
    )
    for name in ("panel.mjs", "panel.css", "callback.mjs", "data.mjs", "session.mjs"):
        shutil.copyfile(ROOT / "integrations/browser" / name, account / name)
    for name in ("sync.mjs", "oidc-client.mjs"):
        shutil.copyfile(ROOT / "account/static/js" / name, account / name)
    (account / "config.mjs").write_text(
        "// Public PKCE client; no secrets.\nexport const config = "
        + json.dumps(
            dict(issuer=issuer, app="searxng", clientId=client_id, scope="openid profile sync"),
            indent=2,
        )
        + ";\n"
    )
    shutil.copyfile(
        ROOT / "integrations/browser/account-callback.html", target / "account-callback.html"
    )
    for path in [target, *target.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/searxng")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--client-id")
    group.add_argument("--register", action="store_true")
    parser.add_argument("--issuer", default="https://account.julianverse.de")
    args = parser.parse_args()
    build(args.output, register_client() if args.register else args.client_id, args.issuer)
    print(f"Öffentliche SearXNG-Dateien erstellt: {args.output}")
