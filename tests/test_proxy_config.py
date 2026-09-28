"""Exercise the shipped Nginx template without touching the running proxy."""

import datetime
import shutil
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
