import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = """
http {
    server { listen 80; server_name search.julianverse.de; return 301 https://$host$request_uri; }
    server {
        listen 443 ssl;
        server_name search.julianverse.de;
        location / {
            proxy_pass http://127.0.0.1:8085;
            proxy_set_header Host $host;
        }
    }
    server { listen 443 ssl; server_name other.test; location / { return 200 'other'; } }
}
"""


def test_proxy_is_idempotent_and_keeps_other_services():
    patch = module("setup-searxng").patch_nginx
    changed = patch(CONFIG)
    assert patch(changed) == changed
    assert "server_name other.test; location / { return 200 'other'; }" in changed
    assert "proxy_set_header Host $host;" in changed
    assert changed.count("sub_filter '</head>'") == 1
    assert "access_log off;" in changed
    assert "text/javascript mjs;" in changed
    assert 'Cache-Control "no-store"' in changed
    assert 'Referrer-Policy "no-referrer"' in changed


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.replace("127.0.0.1:8085", "127.0.0.1:9000"),
        lambda c: c.replace("proxy_pass", 'sub_filter "a" "b"; proxy_pass'),
        lambda c: c.replace("server_name search.julianverse.de", "server_name missing.test"),
    ],
)
def test_unexpected_proxy_is_rejected(change):
    with pytest.raises(RuntimeError):
        module("setup-searxng").patch_nginx(change(CONFIG))


def test_build_contains_only_public_browser_assets(tmp_path):
    module("build-searxng-integration").build(tmp_path, "test-public-client")
    config = (tmp_path / "account/config.mjs").read_text()
    assert "test-public-client" in config and '"app": "searxng"' in config
    assert not any(path.name.startswith(".") for path in tmp_path.rglob("*"))
    assert "./account/data.mjs" in (tmp_path / "adapter.mjs").read_text()
    assert (tmp_path / "account-callback.html").is_file()
    assert (tmp_path / "account/sync.mjs").is_file()
