"""Real static apps + real OIDC/API; ownCloud files remain isolated in memory.

Set JULIANVERSE_STARTPAGE_CHECKOUT and JULIANVERSE_WEATHER_CHECKOUT to app checkouts.
"""

import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest
from flask import Flask, Response, send_from_directory
from werkzeug.serving import WSGIRequestHandler, make_server

playwright = pytest.importorskip("playwright.sync_api")


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


@pytest.mark.parametrize(
    "slug,dirname", [("startpage", "startpage"), ("weather", "julianverse-weather")]
)
def test_static_app_optional_login_sync_offline_and_conflict(app, monkeypatch, slug, dirname):
    root = Path(os.environ.get(f"JULIANVERSE_{slug.upper()}_CHECKOUT", f"/home/srvmgr/{dirname}"))
    if not (root / "account/app.mjs").exists():
        pytest.skip("An app checkout with the browser integration is required")
    from account.extensions import db
    from account.models import Client, CloudConnection, SyncPreference, User
    from account.security import encrypt

    remote = {}
    writes = []
    counter = [0]
    delay = [0]

    def dav(connection, method, url, **kwargs):
        if method == "MKCOL":
            return httpx.Response(405)
        name = url.rsplit("/", 1)[-1]
        current = remote.get(name)
        if method == "GET":
            time.sleep(delay[0])
            return (
                httpx.Response(200, json=current[0], headers={"ETag": current[1]})
                if current
                else httpx.Response(404)
            )
        assert method == "PUT"
        headers = kwargs["headers"]
        if current and headers.get("If-Match") != current[1]:
            return httpx.Response(412)
        if not current and headers.get("If-None-Match") != "*":
            return httpx.Response(412)
        counter[0] += 1
        payload = json.loads(kwargs["content"])
        etag = f'"v{counter[0]}"'
        remote[name] = (payload, etag)
        writes.append((name, payload))
        return httpx.Response(201, headers={"ETag": etag})

    monkeypatch.setattr("account.cloud.dav", dav)
    account_server = make_server(
        "127.0.0.1", 0, app, threaded=True, ssl_context="adhoc", request_handler=QuietHandler
    )
    issuer = f"https://localhost:{account_server.server_port}"
    app.config.update(BASE_URL=issuer, SERVER_NAME=f"localhost:{account_server.server_port}")
    static = Flask("static-app")

    @static.get(f"/{slug}/")
    def index():
        return send_from_directory(root, "index.html")

    @static.get(f"/{slug}/account/config.mjs")
    def config():
        return Response(
            "export const config = "
            + json.dumps(dict(issuer=issuer, app=slug, clientId=f"{slug}-client"))
            + ";",
            mimetype="text/javascript",
        )

    @static.get(f"/{slug}/<path:filename>")
    def asset(filename):
        return send_from_directory(root, filename)

    static_server = make_server(
        "127.0.0.1", 0, static, threaded=True, ssl_context="adhoc", request_handler=QuietHandler
    )
    origin = f"https://localhost:{static_server.server_port}"
    with app.app_context():
        user = db.session.scalar(db.select(User))
        db.session.add(
            CloudConnection(user_id=user.id, username="test-cloud", secret=encrypt("test-only"))
        )
        client = db.session.scalar(db.select(Client).where(Client.slug == slug))
        if not client:
            client = Client(slug=slug, client_id=f"{slug}-client", client_secret="")
            db.session.add(client)
        client.set_client_metadata(
            dict(
                client_name=slug,
                redirect_uris=[f"{origin}/{slug}/account-callback.html"],
                scope="openid profile email sync",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            )
        )
        resources = ["notes"] if slug == "startpage" else ["settings", "locations"]
        for resource in resources:
            db.session.add(
                SyncPreference(user_id=user.id, app_slug=slug, resource=resource, enabled=True)
            )
        db.session.commit()
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (account_server, static_server)
    ]
    for thread in threads:
        thread.start()
    try:
        with playwright.sync_playwright() as engine:
            browser = engine.chromium.launch(args=["--disable-gpu", "--ignore-certificate-errors"])
            context = browser.new_context(
                ignore_https_errors=True, viewport={"width": 1440, "height": 1050}, locale="de-DE"
            )
            context.add_init_script("""if (!localStorage.getItem('test-seeded')) {
              localStorage.setItem('test-seeded', 'true');
              localStorage.setItem('onboarding.done', 'true');
              localStorage.setItem('ui.locale', JSON.stringify('de-de'));
              localStorage.setItem('notes', JSON.stringify('Meine lokale Notiz'));
              localStorage.setItem('widgets', JSON.stringify({todo:true,notes:true,tiles:true,weather:false,transport:false,quote:false,recent:false,system:false,news:false}));
              localStorage.setItem('julianverse-weather:settings', JSON.stringify({language:'de',theme:'light',rainNotifications:false}));
            }""")
            # External weather/news/agent providers are outside the sync contract.
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith((origin, issuer))
                    else route.fulfill(status=503, content_type="application/json", body="{}")
                ),
            )
            page = context.new_page()
            errors = []
            refreshes = []
            page.on(
                "request",
                lambda request: (
                    refreshes.append(True)
                    if request.url == issuer + "/oauth/token"
                    and "grant_type=refresh_token" in (request.post_data or "")
                    else None
                ),
            )
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())

            def open_account():
                page.wait_for_function("window.julianverseReady === true")
                if slug == "startpage":
                    page.locator("#openSettings").click()
                    page.get_by_role("tab", name="Account", exact=True).click()
                else:
                    page.locator(".jv-account summary").click()

            def edit_note(value):
                page.locator("#closeSettings").click()
                playwright.expect(page.locator("#settingsModal")).to_be_hidden()
                page.locator("#notesArea").fill(value)
                open_account()

            page.goto(f"{origin}/{slug}/")
            open_account()
            login = page.get_by_role("button", name="Mit Julianverse anmelden")
            playwright.expect(login).to_be_visible(timeout=20000)
            assert not writes
            with page.expect_popup() as popup_info:
                login.click()
            popup = popup_info.value
            popup.get_by_label("E-Mail oder Benutzername").fill("julian")
            popup.get_by_label("Passwort", exact=True).fill("a-long-test-password")
            popup.get_by_role("button", name="Anmelden", exact=True).click()
            popup.get_by_role("button", name="Zugriff erlauben").click()
            playwright.expect(page.get_by_text("Angemeldet als julian", exact=True)).to_be_visible(
                timeout=15000
            )
            assert not writes
            # A forged callback message cannot replace the session.
            page.evaluate(
                "window.postMessage({type:'julianverse:callback',url:location.href}, location.origin)"
            )
            playwright.expect(page.get_by_text("Angemeldet als julian", exact=True)).to_be_visible()
            resource = "notes" if slug == "startpage" else "settings"
            row = page.locator(f'.jv-resource[data-resource="{resource}"]')
            row.get_by_role("button", name="Lokale Daten hochladen", exact=True).click()
            playwright.expect(row.locator(".jv-status")).to_contain_text(
                "Abgeglichen", timeout=10000
            )
            assert len(writes) == 1
            if slug == "startpage":
                denied = page.locator('.jv-resource[data-resource="tasks"]')
                denied.get_by_role("button", name="Lokale Daten hochladen", exact=True).click()
                playwright.expect(denied.locator(".jv-status")).to_contain_text("nicht freigegeben")
                assert len(writes) == 1
            name = f"{resource}.json"
            assert name in remote
            # Expired browser access tokens are refreshed with rotation, without losing local work.
            page.evaluate(
                "Date.now = (() => { const now = Date.now; return () => now() + 590000; })()"
            )
            row.get_by_role("button", name="Jetzt abgleichen").click()
            playwright.expect(row.locator(".jv-status")).to_contain_text("Abgeglichen")
            assert len(refreshes) == 1
            context.set_offline(True)
            if slug == "startpage":
                edit_note("Offline geändert")
            else:
                page.locator("#theme-select").select_option("dark")
            page.wait_for_timeout(1200)
            assert len(writes) == 1
            context.set_offline(False)
            playwright.expect(row.locator(".jv-status")).to_contain_text(
                "Abgeglichen", timeout=10000
            )
            expected = (
                {"notes": "Offline geändert"}
                if slug == "startpage"
                else {"julianverse-weather:settings": {"theme": "dark"}}
            )
            for key, value in expected.items():
                if isinstance(value, dict):
                    assert remote[name][0]["data"][key]["theme"] == "dark"
                else:
                    assert remote[name][0]["data"][key] == value
            # A concurrent change must become a visible conflict; neither version is lost.
            cloud = json.loads(json.dumps(remote[name][0]))
            if slug == "startpage":
                cloud["data"]["notes"] = "Vom zweiten Gerät"
                edit_note("Mein neuer lokaler Stand")
            else:
                cloud["data"]["julianverse-weather:settings"]["theme"] = "light"
                page.locator("#units-select").select_option("imperial")
            remote[name] = (cloud, '"other-device"')
            playwright.expect(row.locator(".jv-conflict")).to_be_visible(timeout=10000)
            row.get_by_role("button", name="Cloud-Version verwenden", exact=True).click()
            playwright.expect(row.locator(".jv-conflict")).to_have_count(0)
            playwright.expect(row.locator(".jv-status")).to_contain_text(
                "Abgeglichen", timeout=10000
            )
            if slug == "startpage":
                assert page.locator("#notesArea").input_value() == "Vom zweiten Gerät"
            else:
                assert page.locator("#theme-select").input_value() == "light"
                assert page.locator("#units-select").input_value() == "metric"
            # Explicit download from an existing file is also available after stopping sync.
            row.get_by_role("button", name="Sync ausschalten").click()
            row.get_by_role("button", name="Cloud-Daten übernehmen", exact=True).click()
            playwright.expect(row.locator(".jv-status")).to_contain_text("Abgeglichen")
            if slug == "weather":
                locations = page.locator('.jv-resource[data-resource="locations"]')
                locations.get_by_role("button", name="Cloud-Daten übernehmen", exact=True).click()
                playwright.expect(locations.locator(".jv-status")).to_contain_text(
                    "Keine Cloud-Datei"
                )
                place = dict(
                    name="Hannover", latitude=52.37, longitude=9.73, timezone="Europe/Berlin"
                )
                remote["locations.json"] = (
                    dict(
                        schemaVersion=1,
                        deleted=False,
                        data={
                            "julianverse-weather:saved-locations": [place],
                            "julianverse-weather:pinned-location": place,
                        },
                    ),
                    '"places-1"',
                )
                locations.get_by_role("button", name="Cloud-Daten übernehmen", exact=True).click()
                playwright.expect(locations.locator(".jv-status")).to_contain_text("Abgeglichen")
                assert (
                    page.locator("#saved-locations .saved-location-load").inner_text() == "Hannover"
                )
            if slug == "startpage":
                # A local edit arriving while GET is in flight must survive the download.
                remote[name] = (
                    dict(schemaVersion=1, deleted=False, data={"notes": "Remote während GET"}),
                    '"delayed"',
                )
                delay[0] = 1
                row.get_by_role("button", name="Jetzt abgleichen").click()
                edit_note("Während Download bearbeitet")
                playwright.expect(row.locator(".jv-conflict")).to_be_visible(timeout=15000)
                assert page.locator("#notesArea").input_value() == "Während Download bearbeitet"
                delay[0] = 0
                row.get_by_role("button", name="Lokale Version verwenden", exact=True).click()
                playwright.expect(row.locator(".jv-status")).to_contain_text("Abgeglichen")
                assert remote[name][0]["data"]["notes"] == "Während Download bearbeitet"
            captures = Path("test-results")
            captures.mkdir(exist_ok=True)
            page.screenshot(path=str(captures / f"{slug}-sync-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            if slug == "weather":
                page.locator("#sidebar-toggle").click()
                page.wait_for_timeout(350)
                page.locator(".jv-account summary").scroll_into_view_if_needed()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(captures / f"{slug}-sync-mobile.png"), full_page=True)
            row.get_by_role("button", name="Sync ausschalten").click()
            count = len(writes)
            page.wait_for_timeout(1000)
            assert len(writes) == count
            # Another tab claiming a different account stops this tab before any further upload.
            second = context.new_page()
            second.goto(f"{origin}/{slug}/account-callback.html")
            second.evaluate(
                "([app]) => localStorage.setItem(`julianverse:${app}:sync-owner`, 'another-user')",
                [slug],
            )
            playwright.expect(
                page.get_by_role("button", name="Mit Julianverse anmelden")
            ).to_be_visible()
            assert len(writes) == count
            second.close()
            page.reload()
            if slug == "weather":
                page.locator("#sidebar-toggle").click()
            open_account()
            playwright.expect(
                page.get_by_role("button", name="Mit Julianverse anmelden")
            ).to_be_visible()
            assert len(writes) == count
            if slug == "weather":
                page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=10000)
                cached = page.evaluate(
                    "(async () => (await Promise.all((await caches.keys()).map(async name => (await (await caches.open(name)).keys()).map(r => r.url)))).flat())()"
                )
                assert not any(
                    "account-callback" in url or "code=" in url or "state=" in url for url in cached
                )
                assert any("account/app.mjs" in url for url in cached)
                context.set_offline(True)
                page.reload()
                page.locator("#sidebar-toggle").click()
                page.locator(".jv-account summary").click()
                playwright.expect(
                    page.get_by_role("button", name="Mit Julianverse anmelden")
                ).to_be_visible()
                assert (
                    page.locator("#saved-locations .saved-location-load").inner_text() == "Hannover"
                )
            assert not errors, errors
            browser.close()
    finally:
        for server in (account_server, static_server):
            server.shutdown()
        for thread in threads:
            thread.join(timeout=3)
