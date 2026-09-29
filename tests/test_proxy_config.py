"""Exercise the shipped Nginx template without touching the running proxy."""

import datetime
import http.client
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.mark.skipif(not shutil.which("nginx"), reason="Nginx is optional for local tests")
def test_https_maintenance_without_backend(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (tmp_path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    template = (Path(__file__).resolve().parents[1] / "deploy/account.nginx.conf").read_text()
    template = (
        template.replace("listen 80;", "listen 127.0.0.1:18098;")
        .replace("listen 443 ssl;", "listen 127.0.0.1:18443 ssl;")
        .replace("http://127.0.0.1:8096", "http://127.0.0.1:18099")
        .replace(
            "/etc/letsencrypt/live/account.julianverse.de/fullchain.pem", str(tmp_path / "cert.pem")
        )
        .replace(
            "/etc/letsencrypt/live/account.julianverse.de/privkey.pem", str(tmp_path / "key.pem")
        )
        .replace("/var/log/nginx/julianverse-account.error.log", "stderr")
    )
    config = tmp_path / "nginx.conf"
    temp_paths = "\n".join(
        f"{kind}_temp_path {tmp_path}/{kind};"
        for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
    )
    config.write_text(
        f"pid {tmp_path}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off;\n{temp_paths}\n{template}\n}}"
    )
    command = ["nginx", "-p", str(tmp_path), "-c", str(config), "-e", "stderr"]
    checked = subprocess.run(command + ["-t"], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    process = subprocess.Popen(
        command + ["-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        for _ in range(30):
            try:
                urllib.request.urlopen(
                    "https://127.0.0.1:18443/healthz",
                    context=ssl._create_unverified_context(),
                    timeout=1,
                )
            except urllib.error.HTTPError as error:
                assert error.code == 503
                assert error.headers["X-Julianverse-Setup"] == "pending"
                assert "eingerichtet" in error.read().decode()
                break
            except urllib.error.URLError:
                time.sleep(0.1)
        else:
            pytest.fail("Test-Nginx nicht erreichbar")
    finally:
        process.terminate()
        process.communicate(timeout=5)


@pytest.mark.skipif(not shutil.which("nginx"), reason="Nginx is optional for local tests")
def test_news_status_and_resource_preflights_are_proxied_without_redirects(tmp_path):
    """Use private Unix sockets: no live proxy, browsers, credentials or TCP ports."""
    frontend = tmp_path / "front.sock"
    upstream = tmp_path / "back.sock"
    template = (Path(__file__).resolve().parents[1] / "deploy/account.nginx.conf").read_text()
    template = template[template.index("server {\n    listen 443 ssl;") :]
    template = re.sub(r"(?m)^\s*(?:ssl_\w+|http2)\s+[^;]+;", "", template)
    template = (
        template.replace("listen 443 ssl;", f"listen unix:{frontend};")
        .replace("http://127.0.0.1:8096", f"http://unix:{upstream}")
        .replace("/var/log/nginx/julianverse-account.error.log", "stderr")
    )
    temp_paths = "\n".join(
        f"{kind}_temp_path {tmp_path}/{kind};"
        for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
    )
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"pid {tmp_path}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off;\n"
        + temp_paths
        + template
        + f"""
        server {{
            listen unix:{upstream};
            add_header Access-Control-Allow-Origin https://julianverse.de always;
            location / {{
                if ($request_method = OPTIONS) {{ return 204; }}
                default_type application/json;
                return 200 '{{"path":"$request_uri"}}';
            }}
        }}
        }}
        """
    )
    process = subprocess.Popen(
        [
            "nginx",
            "-p",
            str(tmp_path),
            "-c",
            str(config),
            "-e",
            "stderr",
            "-g",
            "daemon off; master_process off;",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(30):
            if frontend.exists():
                break
            assert process.poll() is None, process.stderr.read().decode()
            time.sleep(0.05)
        else:
            pytest.fail("Test-Nginx nicht erreichbar")
        for path in ("/api/sync/news", "/api/sync/news/sources", "/api/sync/news/settings"):
            for method in ("OPTIONS", "GET"):
                connection = http.client.HTTPConnection("account.julianverse.de", timeout=2)
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.settimeout(2)
                connection.sock.connect(str(frontend))
                try:
                    connection.request(
                        method,
                        path,
                        headers={
                            "Origin": "https://julianverse.de",
                            "Access-Control-Request-Method": "GET",
                            "Access-Control-Request-Headers": "authorization",
                        },
                    )
                    response = connection.getresponse()
                    assert response.status == (204 if method == "OPTIONS" else 200)
                    assert response.getheader("Location") is None
                    assert (
                        response.getheader("Access-Control-Allow-Origin")
                        == "https://julianverse.de"
                    )
                    if method == "GET":
                        assert response.read().decode() == f'{{"path":"{path}"}}'
                finally:
                    connection.close()
    finally:
        process.terminate()
        process.communicate(timeout=5)
