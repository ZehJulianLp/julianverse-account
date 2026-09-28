import base64
import hashlib
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest
from werkzeug.serving import WSGIRequestHandler, make_server

playwright = pytest.importorskip("playwright.sync_api")


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def test_browser_login_mobile_and_real_passkey_flow(app):
    server = make_server("127.0.0.1", 0, app, ssl_context="adhoc", request_handler=QuietHandler)
    origin = f"https://localhost:{server.server_port}"
    app.config.update(BASE_URL=origin, SERVER_NAME=f"localhost:{server.server_port}")
    callback = f"https://127.0.0.1:{server.server_port}/test-client-callback"
    from account.extensions import db
    from account.models import Client, User

    with app.app_context():
        db.session.scalar(db.select(User)).is_admin = True
        client = db.session.scalar(db.select(Client))
        metadata = dict(client.client_metadata)
        metadata["redirect_uris"] = [callback]
        client.set_client_metadata(metadata)
        db.session.commit()
    app.add_url_rule("/test-client-callback", view_func=lambda: "SSO callback received")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as engine:
            browser = engine.chromium.launch(args=["--disable-gpu"])
            context = browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1440, "height": 1050},
                color_scheme="light",
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/auth/login")
            captures = Path("test-results")
            captures.mkdir(exist_ok=True)
            page.screenshot(path=str(captures / "login-desktop.png"), full_page=True)
            page.get_by_label("E-Mail oder Benutzername").fill("julian")
            page.get_by_label("Passwort", exact=True).fill("a-long-test-password")
            page.get_by_role("button", name="Anmelden", exact=True).click()
            page.wait_for_url("**/overview")
            assert page.get_by_role("heading", name="Hallo, Julian.").is_visible()
            page.screenshot(path=str(captures / "overview-desktop.png"), full_page=True)
            page.get_by_role("link", name="Verwaltung", exact=True).click()
            assert page.get_by_role("heading", name="Benutzer & Dienste").is_visible()
            page.screenshot(path=str(captures / "admin-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(captures / "admin-mobile.png"), full_page=True)
            page.get_by_role("link", name="Verbindungen", exact=True).click()
            assert page.get_by_role("button", name="Vorhandenes Konto verknüpfen").is_visible()
            page.screenshot(path=str(captures / "cloud-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.set_viewport_size({"width": 1440, "height": 1050})
            page.get_by_role("link", name="Sicherheit", exact=True).click()
            cdp = context.new_cdp_session(page)
            cdp.send("WebAuthn.enable")
            cdp.send(
                "WebAuthn.addVirtualAuthenticator",
                {
                    "options": {
                        "protocol": "ctap2",
                        "transport": "internal",
                        "hasResidentKey": True,
                        "hasUserVerification": True,
                        "isUserVerified": True,
                        "automaticPresenceSimulation": True,
                    }
                },
            )
            page.get_by_role("button", name="Passkey hinzufügen").click()
            playwright.expect(page.get_by_text("Mein Gerät", exact=True)).to_be_visible(
                timeout=10000
            )
            assert page.get_by_text("Mein Gerät", exact=True).is_visible(), page.locator(
                ".client-message"
            ).inner_text()
            page.get_by_role("button", name="Abmelden", exact=True).first.click()
            page.wait_for_url("**/auth/login")
            page.get_by_role("button", name="Mit Passkey anmelden").click()
            page.wait_for_url("**/overview")
            # Responsive navigation and forms stay inside the viewport.
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(captures / "overview-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("link", name="Profil", exact=True).click()
            page.get_by_label("Anzeigename").fill("Julian Browser-Test")
            page.get_by_role("button", name="Profil speichern").click()
            assert page.get_by_text("Dein Profil wurde gespeichert.").is_visible()
            verifier = "browser-pkce-verifier-" * 3
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            query = urlencode(
                dict(
                    client_id="startpage-client",
                    redirect_uri=callback,
                    response_type="code",
                    scope="openid profile",
                    state="browser-state",
                    nonce="browser-nonce",
                    code_challenge=challenge,
                    code_challenge_method="S256",
                )
            )
            page.goto(origin + "/oauth/authorize?" + query)
            page.get_by_role("button", name="Zugriff erlauben").click()
            page.wait_for_url("**/test-client-callback?**")
            assert "code=" in page.url and "state=browser-state" in page.url
            assert page.get_by_text("SSO callback received").is_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
