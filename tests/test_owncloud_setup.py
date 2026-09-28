import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "owncloud_setup", Path(__file__).resolve().parents[1] / "scripts/setup-owncloud.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def test_php_runner_loads_image_environment_and_preserves_input(tmp_path):
    entrypoints = tmp_path / "entrypoints"
    entrypoints.mkdir()
    (entrypoints / "10-defaults.sh").write_text('declare -x JV_FIXTURE_VALUE="default"\n')
    (entrypoints / "90 override.sh").write_text('JV_FIXTURE_VALUE+="-override"\n')
    binary = tmp_path / "php"
    binary.write_text("""#!/usr/bin/env python3
import json, os, sys
print(json.dumps({'value': os.environ['JV_FIXTURE_VALUE'], 'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))
""")
    binary.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    env.pop("OWNCLOUD_ENTRYPOINT_INITIALIZED", None)
    marker = tmp_path / "must-not-be-created"
    # The PHP source is an argv value, never interpolated as shell code.
    code = 'echo "$(touch ' + str(marker) + ')";'
    result = subprocess.run(
        ["bash", "-c", setup.PHP_RUNNER, "julianverse-php", str(entrypoints), code],
        input='{"secret":"stdin-only"}',
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["value"] == "default-override"
    assert payload["argv"] == ["-d", "apc.enable_cli=1", "-r", code]
    assert payload["stdin"] == '{"secret":"stdin-only"}'
    assert not marker.exists()


def test_failed_command_reports_step_and_keeps_raw_details_private(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    original = setup.tempfile.mkstemp
    monkeypatch.setattr(setup.tempfile, "mkstemp", lambda **kwargs: original(dir=tmp_path))
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "SQLSTATE[HY000] fixture-output-secret", "fixture-stderr-secret"
        ),
    )
    with pytest.raises(RuntimeError) as failure:
        setup.run(
            ["docker", "exec", "fixture"],
            data="fixture-input-secret",
            step="ownCloud-Konsole prüfen",
        )
    message = str(failure.value)
    assert "ownCloud-Konsole prüfen" in message and "Datenbank" in message and "Exit 1" in message
    assert "fixture-output-secret" not in message and "fixture-stderr-secret" not in message
    (log,) = tmp_path.iterdir()
    assert log.stat().st_mode & 0o777 == 0o600
    assert "fixture-output-secret" in log.read_text()
    assert "fixture-input-secret" not in log.read_text()


def test_php_failure_does_not_print_json_or_stdin(monkeypatch):
    observed = {}

    def fake_run(args, **kwargs):
        observed.update(args=args, **kwargs)
        return "warning containing fixture-secret; not json"

    monkeypatch.setattr(setup, "run", fake_run)
    with pytest.raises(RuntimeError, match="kein gültiges JSON") as failure:
        setup.run_php(
            ["docker", "exec", "fixture"], "echo json_encode([]);", {"secret": "stdin-secret"}
        )
    assert "fixture-secret" not in str(failure.value)
    assert observed["data"] == json.dumps({"secret": "stdin-secret"})
    assert observed["args"][-2] == "/etc/entrypoint.d"
    assert "$_SERVER['SCRIPT_FILENAME']" in observed["args"][-1]


def test_check_only_stops_before_any_setup_changes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup.os, "umask", lambda _: None)
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    monkeypatch.setattr(
        setup.pwd, "getpwuid", lambda _: SimpleNamespace(pw_uid=1000, pw_name="fixture")
    )
    (tmp_path / ".env").write_text(
        "ACCOUNT_BASE_URL=https://account.test\nOWNCLOUD_BASE_URL=https://cloud.test\n"
    )
    commands = []

    def docker_ps(args, **kwargs):
        commands.append(args)
        assert args == [
            "docker",
            "compose",
            "-f",
            "/opt/owncloud/docker-compose.yml",
            "ps",
            "-q",
            "app",
        ]
        return "fixture-container\n"

    monkeypatch.setattr(setup, "run", docker_ps)
    monkeypatch.setattr(
        setup,
        "run_php",
        lambda *args, **kwargs: {
            "version": "10.15.3",
            "oidc": None,
            "appVersion": "",
            "appEnabled": False,
            "paths": [{"path": "/mnt/data/apps", "writable": True}],
            "marker": None,
        },
    )
    monkeypatch.setattr(
        setup.tempfile, "mkdtemp", lambda **kwargs: pytest.fail("check mode must not start setup")
    )
    setup.main(check_only=True)
    assert len(commands) == 1
    assert "Vorprüfung erfolgreich" in capsys.readouterr().out
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_rollback_of_absent_config_sends_json_null(monkeypatch):
    def check_stdin(args, **kwargs):
        assert kwargs["data"] == "null"
        return '{"ok":true}'

    monkeypatch.setattr(setup, "run", check_stdin)
    assert setup.run_php(["docker", "exec", "fixture"], "read_and_restore();", None) == {"ok": True}


def test_commands_without_payload_close_stdin(monkeypatch):
    def check_stdin(args, **kwargs):
        assert kwargs["input"] == ""
        return subprocess.CompletedProcess(args, 0, "done", "")

    monkeypatch.setattr(setup.subprocess, "run", check_stdin)
    assert setup.run(["fixture-command"]) == "done"


@pytest.mark.parametrize(
    "invalid", [None, "normal-login", "pkce", "nonce", "client", "callback", "scope"]
)
def test_sso_uses_effective_route_and_checks_authentication_parameters(invalid):
    cloud, issuer = "https://cloud.test", "https://account.test"
    params = dict(
        client_id="fixture-client",
        scope="openid profile email owncloud",
        redirect_uri=cloud + "/index.php/apps/openidconnect/redirect",
        response_type="code",
        code_challenge="fixture-challenge",
        code_challenge_method="S256",
        nonce="private-nonce",
        state="private-state",
    )
    if invalid == "pkce":
        params["code_challenge_method"] = "plain"
    if invalid == "nonce":
        params.pop("nonce")
    if invalid == "client":
        params["client_id"] = "another-client"
    if invalid == "callback":
        params["redirect_uri"] = "https://unexpected.test/callback"
    if invalid == "scope":
        params["scope"] = "openid profile email"
    location = (
        cloud + "/login"
        if invalid == "normal-login"
        else issuer + "/oauth/authorize?" + urlencode(params)
    )

    def respond(request):
        assert str(request.url) == cloud + "/index.php/apps/openidconnect/redirect"
        return httpx.Response(302, headers={"Location": location})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if invalid is None:
            setup.verify_sso_redirect(client, cloud, issuer, "fixture-client")
        else:
            with pytest.raises(RuntimeError, match="SSO-Prüfung fehlgeschlagen") as failure:
                setup.verify_sso_redirect(client, cloud, issuer, "fixture-client")
            assert "private-state" not in str(failure.value)
            assert "private-nonce" not in str(failure.value)
