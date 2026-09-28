#!/usr/bin/env python3
"""Add the optional search account UI to the existing Nginx proxy, with rollback."""

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "search.julianverse.de"
TARGET = Path("/srv/http/searxng-account")
NGINX = Path("/etc/nginx/nginx.conf")
MARKER = "# BEGIN Julianverse Search"
END = "# END Julianverse Search"
INJECTION = '<link rel="stylesheet" href="/julianverse/search.css"><script type="module" src="/julianverse/app.mjs"></script>'


def block_end(text, start):
    """Find a brace block without treating braces inside comments/quotes as syntax."""
    depth, quote, comment, escape = 0, None, False, False
    for index in range(start, len(text)):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
            continue
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            comment = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise RuntimeError("Unvollständiger Nginx-Block.")


def patch_nginx(text, target=TARGET):
    matches = []
    for match in re.finditer(r"(?m)^\s*server\s*\{", text):
        start = text.index("{", match.start())
        end = block_end(text, start)
        block = text[start:end]
        if re.search(r"server_name\s+search\.julianverse\.de\s*;", block) and re.search(
            r"listen\s+443\s+ssl", block
        ):
            matches.append((start, end, block))
    if len(matches) != 1:
        raise RuntimeError(
            "Es muss genau einen bestehenden HTTPS-Proxy für search.julianverse.de geben."
        )
    start, end, block = matches[0]
    block = re.sub(
        r"\n[ \t]*# BEGIN Julianverse Search.*?# END Julianverse Search[^\n]*\n",
        "",
        block,
        flags=re.S,
    )
    if "julianverse/" in block:
        raise RuntimeError(
            "Der Pfad /julianverse/ wird bereits von einer anderen Konfiguration verwendet."
        )
    locations = list(re.finditer(r"location\s+/\s*\{", block))
    if len(locations) != 1:
        raise RuntimeError("Der bestehende Such-Proxy ist nicht eindeutig.")
    location = locations[0]
    opening = block.index("{", location.start())
    closing = block_end(block, opening)
    proxy = block[opening:closing]
    if not re.search(r"proxy_pass\s+http://127\.0\.0\.1:8085\s*;", proxy):
        raise RuntimeError("Der erwartete SearXNG-Upstream fehlt.")
    if "sub_filter" in proxy or re.search(r"proxy_set_header\s+Accept-Encoding", proxy):
        raise RuntimeError("Vorhandene HTML-/Kompressionsanpassung bitte zuerst prüfen.")
    filter_config = f"""
              {MARKER} filter
              proxy_set_header Accept-Encoding "";
              sub_filter_once on;
              sub_filter '</head>' '{INJECTION}</head>';
              {END} filter
"""
    block = block[: opening + 1] + filter_config + block[opening + 1 :]
    static_config = f"""
          {MARKER} routes
          access_log off;
          error_log /var/log/nginx/julianverse-search.error.log crit;
          location ^~ /julianverse/ {{
              alias {target}/;
              index index.html;
              types {{ text/html html; text/css css; text/javascript mjs; }}
              default_type application/octet-stream;
              add_header Cache-Control "no-store" always;
              add_header Referrer-Policy "no-referrer" always;
              add_header X-Content-Type-Options "nosniff" always;
              add_header Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src https://account.julianverse.de; form-action 'self'; base-uri 'none'; frame-ancestors 'none'" always;
          }}
          {END} routes
"""
    block = block[:1] + static_config + block[1:]
    return text[:start] + block + text[end:]


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError(
            f"Einrichtungsschritt fehlgeschlagen: {args[0]}. Konfiguration wurde nicht übernommen."
        )


def atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".julianverse-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
        shutil.copystat(path, temporary)
        os.chown(temporary, path.stat().st_uid, path.stat().st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fetch(path):
    with urllib.request.urlopen(f"https://{DOMAIN}{path}", timeout=15) as response:
        return response.read().decode(), response.headers


def verify():
    home, _ = fetch("/")
    preferences, _ = fetch("/preferences")
    page, _ = fetch("/julianverse/")
    callback, headers = fetch("/julianverse/account-callback.html")
    module, module_headers = fetch("/julianverse/app.mjs")
    if not all("/julianverse/app.mjs" in html for html in (home, preferences, page)):
        raise RuntimeError("Die Suchseiten haben die Integration noch nicht geladen.")
    if "callback-status" not in callback or headers.get("Cache-Control") != "no-store":
        raise RuntimeError("Der geschützte Anmelde-Callback fehlt.")
    if "AccountPanel" not in module or "javascript" not in module_headers.get("Content-Type", ""):
        raise RuntimeError("Das Browser-Modul wird nicht korrekt ausgeliefert.")


def verify_account():
    request = urllib.request.Request(
        "https://account.julianverse.de/oauth/browser/searxng",
        method="OPTIONS",
        headers={"Origin": "https://search.julianverse.de"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if (
                response.status != 204
                or response.headers.get("Access-Control-Allow-Origin")
                != "https://search.julianverse.de"
            ):
                raise RuntimeError("Die Account-Anbindung für SearXNG ist noch nicht bereit.")
    except urllib.error.URLError:
        raise RuntimeError(
            "Account-Anbindung fehlt. Client vorbereiten und julianverse-account.service neu laden."
        ) from None


def main(check=False):
    if check:
        verify_account()
        verify()
        print("Suchseite, Einstellungen, Account-Bereich und Callback sind erreichbar.")
        return
    if os.geteuid() != 0:
        raise RuntimeError("Bitte mit sudo bash scripts/setup-searxng.sh ausführen.")
    with open("/run/julianverse-search-setup.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = ROOT / "dist/searxng"
        if not (source / "account/config.mjs").is_file():
            raise RuntimeError(
                "Zuerst .venv/bin/python scripts/build-searxng-integration.py --register ausführen."
            )
        run(["nginx", "-t"])
        verify_account()
        original = NGINX.read_text()
        proposed = patch_nginx(original)
        if TARGET.exists() and not (TARGET / ".julianverse-search").exists():
            raise RuntimeError("Das Zielverzeichnis enthält fremde Dateien.")
        backup = Path(tempfile.mkdtemp(prefix="julianverse-search.", dir="/var/backups"))
        shutil.copy2(NGINX, backup / "nginx.conf")
        existed = TARGET.exists()
        if existed:
            shutil.copytree(TARGET, backup / "assets")
        print(f"Sicherung: {backup}", flush=True)
        installed = False
        try:
            installed = True
            TARGET.mkdir(exist_ok=True, mode=0o755)
            shutil.copytree(source, TARGET, dirs_exist_ok=True)
            (TARGET / ".julianverse-search").write_text("Managed by Julianverse Account\n")
            atomic_write(NGINX, proposed)
            run(["nginx", "-t"])
            print("Lade den Such-Proxy neu. Der SearXNG-Container läuft weiter.", flush=True)
            run(["systemctl", "reload", "nginx"])
            for attempt in range(15):
                try:
                    verify()
                    break
                except (RuntimeError, urllib.error.URLError, TimeoutError):
                    if attempt == 14:
                        raise
                    time.sleep(2)
        except BaseException:
            if installed:
                print("Stelle den bisherigen Such-Proxy wieder her.", flush=True)
                atomic_write(NGINX, original)
                if existed:
                    shutil.copytree(backup / "assets", TARGET, dirs_exist_ok=True)
                run(["nginx", "-t"])
                run(["systemctl", "reload", "nginx"])
            raise
        print(f"Fertig: https://{DOMAIN}/julianverse/", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        main(args.check)
    except (RuntimeError, OSError) as error:
        raise SystemExit(f"FEHLER: {error}") from None
