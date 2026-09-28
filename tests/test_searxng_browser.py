"""Real PKCE, persistent login and sync against isolated Account/ownCloud fixtures."""

import html
import json
import threading
import time

import httpx
import pytest
from flask import Flask, Response, redirect, request, send_from_directory
from test_searxng_setup import module
from werkzeug.serving import WSGIRequestHandler, make_server

playwright = pytest.importorskip("playwright.sync_api")


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def test_search_optional_history_preferences_and_real_account_sync(app, monkeypatch, tmp_path):
    from account.extensions import db
    from account.models import Client, CloudConnection, SyncPreference, User
    from account.security import encrypt

    remote, writes = {}, []

    def dav(connection, method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        if method == "MKCOL":
            return httpx.Response(405)
        if method == "GET":
            if name not in remote:
                return httpx.Response(404)
            data, etag = remote[name]
            return httpx.Response(200, json=data, headers={"ETag": etag})
        assert method == "PUT"
        expected = kwargs.get("headers", {}).get("If-Match")
        if expected and (name not in remote or expected != remote[name][1]):
            return httpx.Response(412)
        writes.append(name)
        etag = f'"version-{len(writes)}"'
        remote[name] = json.loads(kwargs["content"]), etag
        return httpx.Response(201, headers={"ETag": etag})

    monkeypatch.setattr("account.cloud.dav", dav)
    account_server = make_server(
        "127.0.0.1", 0, app, threaded=True, ssl_context="adhoc", request_handler=QuietHandler
    )
    issuer = f"https://localhost:{account_server.server_port}"
    app.config.update(BASE_URL=issuer, SERVER_NAME=f"localhost:{account_server.server_port}")
    module("build-searxng-integration").build(tmp_path, "searxng-client", issuer)
    search = Flask("search-fixture")
    search_requests = []

    @search.get("/julianverse/")
    def index():
        return send_from_directory(tmp_path, "index.html")

    @search.get("/julianverse/<path:name>")
    def asset(name):
        return send_from_directory(
            tmp_path, name, mimetype="text/javascript" if name.endswith(".mjs") else None
        )

    @search.get("/static/themes/simple/img/favicon.svg")
    def favicon():
        return Response('<svg xmlns="http://www.w3.org/2000/svg"/>', mimetype="image/svg+xml")

    @search.route("/search", methods=["GET", "POST"])
    @search.get("/")
    def results():
        search_requests.append(dict(request.values))
        q = html.escape(request.values.get("q", ""), quote=True)
        category = html.escape(request.values.get("categories", "images"), quote=True)
        return f'''<!doctype html><html lang="de"><head><meta name="endpoint" content="{"results" if q else "index"}"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/julianverse/search.css"><script type="module" src="/julianverse/app.mjs"></script></head><body><main><nav id="links_on_top"></nav><form id="search" action="/search"><input name="q" value="{q}"><input name="categories" value="{category}"><input name="language" value="de-DE"><input name="time_range" value="week"><input name="safesearch" value="1"><button>Suchen</button></form></main></body></html>'''

    @search.route("/preferences", methods=["GET", "POST"])
    def preferences():
        response = redirect("/julianverse/")
        response.set_cookie("simple_style", "dark")
        response.set_cookie("disabled_engines", "brave__general,google cse__general")
        response.set_cookie("tokens", "never-sync-this-engine-secret")
        return response

    search_server = make_server(
        "127.0.0.1", 0, search, threaded=True, ssl_context="adhoc", request_handler=QuietHandler
    )
    origin = f"https://localhost:{search_server.server_port}"
    with app.app_context():
        user = db.session.scalar(db.select(User))
        db.session.add(
            CloudConnection(user_id=user.id, username="test-cloud", secret=encrypt("test-only"))
        )
        client = Client(slug="searxng", client_id="searxng-client", client_secret="")
        client.set_client_metadata(
            dict(
                client_name="Julianverse Search",
                redirect_uris=[origin + "/julianverse/account-callback.html"],
                scope="openid profile sync",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            )
        )
        db.session.add(client)
        for resource in ("favorites", "settings", "history"):
            db.session.add(
                SyncPreference(user_id=user.id, app_slug="searxng", resource=resource, enabled=True)
            )
        db.session.commit()
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (account_server, search_server)
    ]
    for thread in threads:
        thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            errors, account_requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request",
                lambda req: (
                    account_requests.append(req.url) if req.url.startswith(issuer) else None
                ),
            )
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(origin + "/search?q=Not+recorded")
            playwright.expect(
                page.get_by_role("link", name="Meine Suche", exact=True)
            ).to_be_visible()
            assert page.evaluate("localStorage.getItem('julianverse-search:history')") is None
            assert not account_requests and not writes
            page.get_by_role("button", name="Suche speichern", exact=True).click()
            page.get_by_role("link", name="Meine Suche", exact=True).click()
            playwright.expect(page.locator("#saved-list")).to_contain_text("Not recorded")
            page.locator("#saved-list").get_by_role("button", name="Suchen", exact=True).click()
            assert search_requests[-1]["categories"] == "images"
            assert search_requests[-1]["language"] == "de-DE"
            assert search_requests[-1]["time_range"] == "week"
            page.goto(origin + "/julianverse/")
            page.get_by_role("button", name="Verlauf aktivieren").click()
            page.goto(origin + "/search?q=Recorded")
            page.goto(origin + "/julianverse/")
            playwright.expect(page.locator("#history-list")).to_contain_text("Recorded")
            page.get_by_role("button", name="Verlauf pausieren").click()
            page.goto(origin + "/search?q=Paused")
            page.goto(origin + "/preferences")
            assert "Paused" not in page.locator("#history-list").inner_text()
            assert "never-sync" not in page.evaluate(
                "localStorage.getItem('julianverse-search:settings')"
            )
            page.locator('#save-search [name="q"]').fill('<img src=x onerror="alert(1)">')
            page.locator("#save-search").get_by_role("button").click()
            assert page.locator("#saved-list img").count() == 0
            with page.expect_popup() as popup_info:
                page.get_by_role("button", name="Mit Julianverse anmelden").click()
            popup = popup_info.value
            popup.get_by_label("E-Mail oder Benutzername").fill("julian")
            popup.get_by_label("Passwort", exact=True).fill("a-long-test-password")
            popup.get_by_role("button", name="Anmelden", exact=True).click()
            popup.get_by_role("button", name="Zugriff erlauben").click()
            playwright.expect(page.get_by_text("Angemeldet als julian", exact=True)).to_be_visible(
                timeout=15000
            )
            assert not writes
            for resource in ("favorites", "settings"):
                row = page.locator(f'[data-resource="{resource}"]')
                row.get_by_role("button", name="Lokale Daten hochladen").click()
                playwright.expect(row.locator(".jv-status")).to_contain_text(
                    "Abgeglichen", timeout=15000
                )
            assert "history.json" not in remote
            assert "tokens" not in remote["settings.json"][0]["data"]["cookies"]
            cookie = next(c for c in context.cookies() if c["name"] == "__Secure-jv-app-searxng")
            assert cookie["httpOnly"] and cookie["secure"]
            assert cookie["expires"] > time.time() + 86400
            page.reload()
            playwright.expect(page.get_by_text("Angemeldet als julian", exact=True)).to_be_visible(
                timeout=15000
            )
            row = page.locator('[data-resource="favorites"]')
            playwright.expect(row.locator(".jv-status")).to_contain_text(
                "Abgeglichen", timeout=15000
            )
            row = page.locator('[data-resource="history"]')
            row.get_by_role("button", name="Lokale Daten hochladen").click()
            playwright.expect(row.locator(".jv-status")).to_contain_text(
                "Abgeglichen", timeout=15000
            )
            page.get_by_role("button", name="Verlauf löschen", exact=True).click()
            page.wait_for_function(
                "document.querySelector('[data-resource=history] .jv-status').textContent.includes('Abgeglichen')"
            )
            deadline = time.time() + 15
            while remote["history.json"][0]["data"]["entries"] and time.time() < deadline:
                page.wait_for_timeout(100)
            assert not remote["history.json"][0]["data"]["entries"]
            # A change directly in ownCloud is imported, including upstream cookie encoding.
            remote["settings.json"] = (
                {
                    "schemaVersion": 1,
                    "data": {
                        "cookies": {
                            "simple_style": "light",
                            "disabled_engines": "brave__general,google cse__general",
                        }
                    },
                },
                '"cloud-change"',
            )
            row = page.locator('[data-resource="settings"]')
            row.get_by_role("button", name="Jetzt abgleichen").click()
            page.wait_for_function("document.documentElement.dataset.theme === 'light'")
            assert "never-sync-this-engine-secret" in page.evaluate("document.cookie")
            page.reload()
            playwright.expect(page.get_by_text("Angemeldet als julian", exact=True)).to_be_visible(
                timeout=15000
            )
            # History added on a results page is uploaded after navigation, without re-enabling sync.
            page.get_by_role("button", name="Verlauf aktivieren").click()
            page.goto(origin + "/search?q=After+login")
            page.goto(origin + "/julianverse/")
            playwright.expect(page.locator('[data-resource="history"] .jv-status')).to_contain_text(
                "Abgeglichen", timeout=15000
            )
            assert remote["history.json"][0]["data"]["entries"][0]["q"] == "After login"
            # Concurrent edits are kept until the user explicitly resolves the conflict.
            context.set_offline(True)
            page.locator('#save-search [name="q"]').fill("Offline favorite")
            page.locator("#save-search").get_by_role("button").click()
            cloud_favorites = json.loads(json.dumps(remote["favorites.json"][0]))
            cloud_favorites["data"]["entries"][0]["label"] = "Edited in ownCloud"
            remote["favorites.json"] = cloud_favorites, '"concurrent-cloud-edit"'
            context.set_offline(False)
            favorites_row = page.locator('[data-resource="favorites"]')
            playwright.expect(
                favorites_row.get_by_role("button", name="Lokale Version verwenden")
            ).to_be_visible(timeout=15000)
            assert (
                remote["favorites.json"][0]["data"]["entries"][0]["label"] == "Edited in ownCloud"
            )
            playwright.expect(page.locator("#saved-list")).to_contain_text("Offline favorite")
            favorites_row.get_by_role("button", name="Lokale Version verwenden").click()
            playwright.expect(favorites_row.locator(".jv-status")).to_contain_text(
                "Abgeglichen", timeout=15000
            )
            assert any(
                item["q"] == "Offline favorite"
                for item in remote["favorites.json"][0]["data"]["entries"]
            )
            for width in (320, 390, 768, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert not errors
            assert not any(
                "access_token" in value or "refresh_token" in value
                for value in page.evaluate("Object.values(localStorage)")
            )
            page.get_by_role("button", name="App abmelden").click()
            playwright.expect(
                page.get_by_role("button", name="Mit Julianverse anmelden")
            ).to_be_visible()
            page.reload()
            playwright.expect(
                page.get_by_role("button", name="Mit Julianverse anmelden")
            ).to_be_visible()
            playwright.expect(page.locator("#saved-list")).to_contain_text("Offline favorite")
            browser.close()
    finally:
        account_server.shutdown()
        search_server.shutdown()
        for thread in threads:
            thread.join(timeout=5)
