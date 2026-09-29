#!/usr/bin/env python3
"""Install the News callback headers and allow larger News sync documents."""

import fcntl
import os
import re
import runpy
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / "scripts/setup-searxng.py"))
block_end = helpers["block_end"]
atomic_write = helpers["atomic_write"]
run = helpers["run"]


def patch_account(text):
    if "# Julianverse News sync" in text:
        return text
    anchor = "    location / {\n"
    if text.count(anchor) != 1 or "server_name account.julianverse.de;" not in text:
        raise RuntimeError("Der Account-Proxy hat nicht die erwartete Struktur.")
    start = text.index(anchor) + len("    location / ")
    end = block_end(text, start)
    body = text[start + 1 : end - 1]
    if "proxy_pass http://127.0.0.1:8096;" not in body:
        raise RuntimeError("Der erwartete Account-Upstream fehlt.")
    extra = (
        "    # Julianverse News sync\n    location ^~ /api/sync/news/ {\n        client_max_body_size 8m;"
        + body
        + "}\n\n"
    )
    return text.replace(anchor, extra + anchor, 1)


def patch_main(text):
    if "# Julianverse News callback" in text:
        return text
    matches = []
    for match in re.finditer(r"(?m)^\s*server\s*\{", text):
        opening = text.index("{", match.start())
        closing = block_end(text, opening)
        block = text[opening:closing]
        if re.search(r"server_name\s+julianverse\.de\s*;", block) and re.search(
            r"listen\s+443\s+ssl", block
        ):
            matches.append(opening)
            if "/news/account-callback.html" in block:
                raise RuntimeError("Der News-Callback hat bereits eine fremde Konfiguration.")
    if len(matches) != 1:
        raise RuntimeError("Der HTTPS-Server für julianverse.de ist nicht eindeutig.")
    extra = """
        # Julianverse News callback
        location = /news/account-callback.html {
            root /srv/http;
            access_log off;
            error_log /var/log/nginx/julianverse-news-callback.error.log crit;
            add_header Cache-Control "no-store" always;
            add_header Referrer-Policy "no-referrer" always;
            add_header X-Content-Type-Options "nosniff" always;
        }
"""
    at = matches[0] + 1
    return text[:at] + extra + text[at:]


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Bitte mit sudo bash scripts/setup-news-proxy.sh ausführen.")
    with open("/run/julianverse-news-proxy.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(["nginx", "-t"])
        configs = {
            Path("/etc/nginx/nginx.conf"): patch_main,
            Path("/etc/nginx/conf.d/julianverse-account.conf"): patch_account,
        }
        originals = {path: path.read_text() for path in configs}
        proposed = {path: patch(originals[path]) for path, patch in configs.items()}
        if proposed == originals:
            print("News-Proxy ist bereits eingerichtet.")
            return
        backup = Path(tempfile.mkdtemp(prefix="julianverse-news-proxy.", dir="/var/backups"))
        for path in configs:
            shutil.copy2(path, backup / path.name)
        print(f"Sicherung: {backup}", flush=True)
        try:
            for path, value in proposed.items():
                atomic_write(path, value)
            run(["nginx", "-t"])
            run(["systemctl", "reload", "nginx"])
        except BaseException:
            for path, value in originals.items():
                atomic_write(path, value)
            run(["nginx", "-t"])
            run(["systemctl", "reload", "nginx"])
            raise
        print("News-Callback geschützt; News-Sync erlaubt bis zu 8 MiB pro Datei.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as error:
        raise SystemExit(f"FEHLER: {error}") from None
