"""Root-only setup for the existing ownCloud Docker installation.

No credentials in argv or output. The original password login stays available.
Only accounts created by Julianverse are managed by the provisioning service.
"""

import argparse
import hashlib
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import httpx
from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.3.5"  # Compatible with ownCloud 10.12–10.x, PHP >= 7.4.
SHA256 = "4bc0e74cdd5a8266274dc4e77d24a651f9abc1622f1bd07a74b3652e5c16316a"
BOOTSTRAP = (
    "define('OC_CONSOLE', true); "
    "$_SERVER['SCRIPT_FILENAME'] = '/var/www/owncloud/occ'; "
    "$_SERVER['SCRIPT_NAME'] = '/var/www/owncloud/occ'; "
    "require '/var/www/owncloud/lib/base.php'; "
)
# Match the official ownCloud image's /usr/bin/occ wrapper. docker exec does
# not inherit variables computed by the container's running entrypoint. The
# config depends on these defaults, including OWNCLOUD_VOLUME_APPS.
PHP_RUNNER = r"""set -eo pipefail
set +x
if [[ -z "${OWNCLOUD_ENTRYPOINT_INITIALIZED:-}" ]]; then
    [[ -d "$1" ]] || { echo 'OWNCLOUD_ENTRYPOINT_MISSING' >&2; exit 1; }
    while IFS= read -r entrypoint_script; do
        source "$entrypoint_script"
    done < <(find "$1" -iname '*.sh' -type f | sort)
fi
exec php -d apc.enable_cli=1 -r "$2"
"""


def error_hint(output):
    """Only fixed, non-secret descriptions ever reach the terminal."""
    lowered = output.lower()
    if "permission denied" in lowered and ("docker.sock" in lowered or "docker daemon" in lowered):
        return "Zugriff auf den Docker-Dienst verweigert."
    if "cannot connect to the docker daemon" in lowered:
        return "Der Docker-Dienst ist nicht erreichbar."
    if "is not a docker command" in lowered or "unknown shorthand flag" in lowered:
        return "Docker Compose ist in dieser Umgebung nicht verfügbar."
    if "owncloud_entrypoint_missing" in lowered:
        return "Die Initialisierungsskripte des ownCloud-Images fehlen."
    if "app directory" in lowered and "not found" in lowered:
        return "Ein ownCloud-App-Verzeichnis fehlt in der Konsolenumgebung."
    if "sqlstate[" in lowered or "failed to connect to the database" in lowered:
        return "ownCloud kann in der Konsolenumgebung nicht auf die Datenbank zugreifen."
    if "cannot write into" in lowered and "config" in lowered:
        return "Die ownCloud-Konsole kann das Konfigurationsverzeichnis nicht schreiben."
    if "not compatible with php" in lowered:
        return "Die PHP-Version der Konsole passt nicht zu ownCloud."
    if "no such container" in lowered or "is not running" in lowered:
        return "Der ownCloud-Container läuft nicht oder wurde zwischenzeitlich ersetzt."
    return "Details stehen ausschließlich in der privaten Fehlerdatei."


def run(args, *, data=None, step=None):
    step = step or ("ownCloud-Containerbefehl" if args[0] == "docker" else "Account-Dienstbefehl")
    try:
        result = subprocess.run(
            args,
            input=data if data is not None else "",
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(step + ": Zeitlimit von 180 Sekunden überschritten.") from None
    except OSError:
        raise RuntimeError(
            step + ": Das benötigte Programm konnte nicht gestartet werden."
        ) from None
    if result.returncode:
        # Bootstrap/SQL diagnostics can contain secrets. Never print raw output
        # or stdin; store diagnostics only in a new root-private 0600 file.
        output = result.stdout + "\n" + result.stderr
        hint = error_hint(output)
        log_note = ""
        if os.geteuid() == 0:
            try:
                fd, filename = tempfile.mkstemp(
                    prefix="julianverse-owncloud-error-", suffix=".log", dir="/var/log"
                )
                with os.fdopen(fd, "w") as stream:
                    stream.write(f"Schritt: {step}\nExit: {result.returncode}\n" + output)
                log_note = " Private Fehlerdatei (nur root): " + filename
            except OSError:
                log_note = " Die private Fehlerdatei konnte nicht geschrieben werden."
        raise RuntimeError(f"{step} fehlgeschlagen (Exit {result.returncode}). {hint}{log_note}")
    return result.stdout


def run_php(docker, code, payload=None, *, step="ownCloud-Konfiguration ausführen"):
    output = run(
        docker
        + ["bash", "-c", PHP_RUNNER, "julianverse-php", "/etc/entrypoint.d", BOOTSTRAP + code],
        # None is JSON null, not an inherited terminal. The rollback reads
        # STDIN too and must receive EOF when the previous config was absent.
        data=json.dumps(payload),
        step=step,
    )
    try:
        return json.loads(output)
    except ValueError:
        raise RuntimeError(step + ": Die Konsole hat kein gültiges JSON geliefert.") from None


def verify_sso_redirect(client, cloud, issuer, client_id):
    # ownCloud's router registers loginFlow#login twice. The later /redirect
    # route is the effective public route, for both login start and callback.
    callback = cloud + "/index.php/apps/openidconnect/redirect"
    response = client.get(callback)
    location = urlsplit(response.headers.get("location", ""))
    params = parse_qs(location.query)
    checks = {
        "HTTP-Weiterleitung": response.status_code in (302, 303),
        "Account-Ziel": location.scheme + "://" + location.netloc + location.path
        == issuer + "/oauth/authorize",
        "PKCE S256": params.get("code_challenge_method") == ["S256"]
        and len(params.get("code_challenge", [])) == 1
        and bool(params["code_challenge"][0]),
        "Nonce": len(params.get("nonce", [])) == 1 and bool(params["nonce"][0]),
        "State": len(params.get("state", [])) == 1 and bool(params["state"][0]),
        "Authorization Code": params.get("response_type") == ["code"],
        "Client-ID": params.get("client_id") == [client_id],
        "Callback": params.get("redirect_uri") == [callback],
        "ownCloud-Freigabe": "owncloud" in params.get("scope", [""])[0].split(),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        # Report checks, not full redirect URLs: these contain session values.
        raise RuntimeError(
            f"ownCloud-SSO-Prüfung fehlgeschlagen (HTTP {response.status_code}): "
            + ", ".join(failed)
        )


def verify_provisioning(cloud, service_user, password, group):
    """Exercise actual group-admin permissions using one disposable empty account."""
    username = "jv_probe_" + secrets.token_hex(12)
    probe_password = secrets.token_urlsafe(48)
    with httpx.Client(
        timeout=20, follow_redirects=False, trust_env=False, auth=(service_user, password)
    ) as client:

        def api(method, path, data=None):
            response = client.request(
                method,
                cloud + "/ocs/v1.php/cloud/" + path,
                params={"format": "json"},
                headers={"OCS-APIRequest": "true"},
                data=data,
            )
            response.raise_for_status()
            result = response.json()["ocs"]
            if int(result["meta"]["statuscode"]) != 100:
                raise RuntimeError(
                    "Die ownCloud-Probe konnte eine Verwaltungsaktion nicht ausführen."
                )
            return result.get("data") or {}

        api("POST", "users", {"userid": username, "password": probe_password, "groups[]": group})
        # Delete only after a confirmed successful creation of this random name.
        try:
            path = "users/" + username
            api("PUT", path, {"key": "quota", "value": "1 GB"})
            info = api("GET", path)
            if str(info.get("quota", {}).get("definition", "")).replace(" ", "").upper() != "1GB":
                raise RuntimeError("Die 1-GB-Quota wurde von ownCloud nicht bestätigt.")
            with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as dav_client:
                response = dav_client.request(
                    "PROPFIND",
                    cloud + "/remote.php/dav/",
                    auth=(username, probe_password),
                    headers={"Depth": "0", "Content-Type": "application/xml"},
                    content=b'<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>',
                )
            if response.status_code != 207:
                raise RuntimeError("Die WebDAV-Probe ist fehlgeschlagen.")
            tree = ElementTree.fromstring(response.content)
            principals = [
                el.text for el in tree.findall(".//{DAV:}current-user-principal/{DAV:}href")
            ]
            if not any(p and p.endswith("/principals/users/" + username + "/") for p in principals):
                raise RuntimeError("Die ownCloud-Identitätsprüfung ist fehlgeschlagen.")
            api("PUT", path + "/disable")
            if str(api("GET", path).get("enabled")).lower() != "false":
                raise RuntimeError("Die Cloud-Sperre wurde nicht bestätigt.")
            api("PUT", path + "/enable")
            if str(api("GET", path).get("enabled")).lower() != "true":
                raise RuntimeError("Das Entsperren wurde nicht bestätigt.")
        finally:
            try:
                api("DELETE", "users/" + username)
            except Exception:
                raise RuntimeError(
                    "Das leere Prüfkonto muss in ownCloud entfernt werden: " + username
                ) from None


def main(check_only=False):
    if os.geteuid() != 0:
        raise RuntimeError("Bitte mit sudo ausführen.")
    os.umask(0o077)
    account = pwd.getpwuid((ROOT / ".env").stat().st_uid)
    if account.pw_uid == 0:
        raise RuntimeError("Die Account-Konfiguration muss einem normalen Benutzer gehören.")
    user_env = [
        "runuser",
        "-u",
        account.pw_name,
        "--",
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{account.pw_uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{account.pw_uid}/bus",
    ]
    print("Suche den laufenden ownCloud-Container.", flush=True)
    container = run(
        ["docker", "compose", "-f", "/opt/owncloud/docker-compose.yml", "ps", "-q", "app"],
        step="ownCloud-Container über Docker Compose finden",
    ).strip()
    if not container or "\n" in container:
        raise RuntimeError("Der laufende ownCloud-App-Container wurde nicht eindeutig gefunden.")
    docker = [
        "docker",
        "exec",
        "-i",
        "--user",
        "www-data",
        "--workdir",
        "/var/www/owncloud",
        container,
    ]

    def php(code, payload=None, **kwargs):
        return run_php(docker, code, payload, **kwargs)

    print("Prüfe ownCloud mit der initialisierten Docker-Konsolenumgebung.", flush=True)
    snapshot = php(
        """$c=\\OC::$server->getConfig(); echo json_encode([
      'version'=>\\OC_Util::getVersionString(),
      'oidc'=>$c->getSystemValue('openid-connect', null),
      'appVersion'=>$c->getAppValue('openidconnect','installed_version',''),
      'appEnabled'=>\\OC::$server->getAppManager()->isEnabledForUser('openidconnect'),
      'paths'=>\\OC::$APPSROOTS,
      'marker'=>$c->getSystemValue('julianverse-account-provisioner', null)
    ], JSON_THROW_ON_ERROR);""",
        step="ownCloud-Konsole starten und Konfiguration lesen",
    )
    if not snapshot["version"].startswith("10.") or int(snapshot["version"].split(".")[1]) < 12:
        raise RuntimeError("Dieses Skript unterstützt ownCloud Server 10.12–10.x.")
    cfg = dotenv_values(ROOT / ".env")
    issuer = cfg["ACCOUNT_BASE_URL"].rstrip("/")
    cloud = cfg.get("OWNCLOUD_BASE_URL", "https://cloud.julianverse.de").rstrip("/")
    if not issuer.startswith("https://") or not cloud.startswith("https://"):
        raise RuntimeError("Account und ownCloud müssen HTTPS verwenden.")
    if snapshot["oidc"] and snapshot["oidc"].get("provider-url") != issuer:
        raise RuntimeError(
            "Ein anderer OIDC-Anbieter ist bereits eingerichtet. Seine Konfiguration bleibt erhalten."
        )
    if snapshot["appVersion"] not in ("", VERSION):
        raise RuntimeError(
            "Vorhandene OIDC-App bitte separat prüfen; dieses Skript installiert Version "
            + VERSION
            + "."
        )
    paths = [p["path"] for p in snapshot["paths"] if p.get("writable")]
    if len(paths) != 1 or not paths[0].startswith("/") or ".." in paths[0].split("/"):
        raise RuntimeError("Das beschreibbare ownCloud-App-Verzeichnis ist nicht eindeutig.")
    if check_only:
        print(
            "Vorprüfung erfolgreich: ownCloud "
            + snapshot["version"]
            + ". Es wurde nichts eingerichtet."
        )
        return
    backup = Path(tempfile.mkdtemp(prefix="julianverse-account-owncloud.", dir="/var/backups"))
    (backup / "owncloud-before.json").write_text(json.dumps(snapshot, indent=2))
    shutil.copy2(ROOT / ".env", backup / "account.env")
    print("Sicherung:", backup, flush=True)
    os.chdir(ROOT)
    run(user_env + [str(ROOT / ".venv/bin/flask"), "--app", "wsgi", "prepare-owncloud"])
    bundle = json.loads((ROOT / "instance/owncloud-setup.json").read_text())
    crypt = Fernet(cfg["ACCOUNT_ENCRYPTION_KEY"].encode())
    password = crypt.decrypt(bundle["provision_password"].encode()).decode()
    oidc_secret = crypt.decrypt(bundle["oidc_secret"].encode()).decode()
    group = "julianverse-account"
    marker = {"user": bundle["provision_user"], "group": group}
    if snapshot["marker"] and snapshot["marker"] != marker:
        raise RuntimeError("Eine abweichende Julianverse-Cloud-Einrichtung existiert bereits.")

    if not snapshot["appVersion"]:
        print("Installiere die geprüfte OIDC-App für ownCloud 10.", flush=True)
        archive = backup / "openidconnect.tar.gz"
        with httpx.Client(follow_redirects=True, timeout=60) as client:
            response = client.get(
                f"https://github.com/owncloud/openidconnect/releases/download/v{VERSION}/openidconnect.tar.gz"
            )
            response.raise_for_status()
            if hashlib.sha256(response.content).hexdigest() != SHA256:
                raise RuntimeError("Die Prüfsumme der OIDC-App stimmt nicht.")
            archive.write_bytes(response.content)
        # Refuse to replace an existing unregistered directory.
        run(["docker", "exec", container, "test", "!", "-e", paths[0] + "/openidconnect"])
        remote_archive = (
            "/tmp/julianverse-openidconnect-" + backup.name.rsplit(".", 1)[-1] + ".tar.gz"
        )
        run(["docker", "cp", str(archive), container + ":" + remote_archive])
        try:
            run(["docker", "exec", container, "tar", "-xzf", remote_archive, "-C", paths[0]])
            run(
                [
                    "docker",
                    "exec",
                    container,
                    "chown",
                    "-R",
                    "www-data:www-data",
                    paths[0] + "/openidconnect",
                ]
            )
        finally:
            run(["docker", "exec", container, "rm", "-f", remote_archive])

    print("Richte den auf neue Cloud-Konten begrenzten Verwaltungszugang ein.", flush=True)
    result = php(
        """$p=json_decode(stream_get_contents(STDIN),true,512,JSON_THROW_ON_ERROR);
      $c=\\OC::$server->getConfig(); $um=\\OC::$server->getUserManager(); $gm=\\OC::$server->getGroupManager();
      $old=$c->getSystemValue('julianverse-account-provisioner', null);
      if (!$old && ($um->userExists($p['user']) || $gm->groupExists($p['group']))) {
        echo json_encode(['ok'=>false,'reason'=>'Namenskollision beim Dienstkonto oder der Gruppe.']); exit;
      }
      if ($old && $old !== ['user'=>$p['user'],'group'=>$p['group']]) {
        echo json_encode(['ok'=>false,'reason'=>'Abweichende Dienstkonto-Konfiguration.']); exit;
      }
      $c->setSystemValue('julianverse-account-provisioner',['user'=>$p['user'],'group'=>$p['group']]);
      $g=$gm->get($p['group']) ?: $gm->createGroup($p['group']);
      $u=$um->get($p['user']) ?: $um->createUser($p['user'],$p['password']);
      if ($gm->isAdmin($u->getUID())) { echo json_encode(['ok'=>false,'reason'=>'Dienstkonto darf kein globaler ownCloud-Admin sein.']); exit; }
      $u->setPassword($p['password']); $u->setQuota('1 MB');
      $u->setDisplayName('Julianverse Account Provisionierung');
      if (!$gm->getSubAdmin()->isSubAdminofGroup($u,$g)) { $gm->getSubAdmin()->createSubAdmin($u,$g); }
      echo json_encode(['ok'=>true]);""",
        dict(user=bundle["provision_user"], group=group, password=password),
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("reason", "Dienstkonto konnte nicht angelegt werden."))

    oidc = {
        "provider-url": issuer,
        "client-id": bundle["client_id"],
        "client-secret": oidc_secret,
        "loginButtonName": "Julianverse Account",
        "autoRedirectOnLoginPage": False,
        "mode": "userid",
        "search-attribute": "owncloud_username",
        "auto-provision": {"enabled": False},
        "redirect-url": cloud + "/index.php/apps/openidconnect/redirect",
        "scopes": ["openid", "profile", "email", "owncloud"],
    }
    try:
        php(
            "$p=json_decode(stream_get_contents(STDIN),true,512,JSON_THROW_ON_ERROR); "
            "\\OC::$server->getConfig()->setSystemValue('openid-connect',$p); echo json_encode(['ok'=>true]);",
            oidc,
        )
        run(docker + ["occ", "app:enable", "provisioning_api"])
        run(docker + ["occ", "app:enable", "openidconnect"])
        print(
            "Prüfe Anlage, 1-GB-Quota, Verknüpfung und Sperren mit einem leeren Prüfkonto.",
            flush=True,
        )
        verify_provisioning(cloud, bundle["provision_user"], password, group)
        # Verify real HTTPS discovery/login, including PKCE, without logging in
        # as a real user or copying any existing files.
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            discovery = client.get(issuer + "/.well-known/openid-configuration")
            discovery.raise_for_status()
            if discovery.json().get("introspection_endpoint") != issuer + "/oauth/introspect":
                raise RuntimeError("Bitte zuerst die neue Account-Version installieren.")
            verify_sso_redirect(client, cloud, issuer, bundle["client_id"])
            response = client.get(
                cloud + "/ocs/v1.php/cloud/users/" + bundle["provision_user"],
                params={"format": "json"},
                headers={"OCS-APIRequest": "true"},
                auth=(bundle["provision_user"], password),
            )
            if response.json().get("ocs", {}).get("meta", {}).get("statuscode") not in (100, "100"):
                raise RuntimeError("Der Verwaltungszugang konnte nicht bestätigt werden.")
        for key, value in {
            "OWNCLOUD_PROVISION_USER": bundle["provision_user"],
            "OWNCLOUD_PROVISION_PASSWORD": password,
            "OWNCLOUD_PROVISION_GROUP": group,
            "OWNCLOUD_SSO_ENABLED": "true",
        }.items():
            set_key(ROOT / ".env", key, value)
        os.chown(ROOT / ".env", account.pw_uid, account.pw_gid)
        os.chmod(ROOT / ".env", 0o600)
        unit_dir = Path(account.pw_dir) / ".config/systemd/user"
        for name in ("julianverse-cloud.service", "julianverse-cloud.timer"):
            dest = unit_dir / name
            dest.write_text((ROOT / "deploy" / name).read_text().replace("@ROOT@", str(ROOT)))
            os.chown(dest, account.pw_uid, account.pw_gid)
        run(user_env + ["systemctl", "--user", "daemon-reload"])
        run(user_env + ["systemctl", "--user", "restart", "julianverse-account.service"])
        for attempt in range(30):
            try:
                ready = httpx.get(issuer + "/healthz", timeout=2, trust_env=False)
                if ready.status_code == 200 and ready.json().get("status") == "ok":
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Die Account-App ist nach dem Neustart nicht erreichbar.")
        run(user_env + ["systemctl", "--user", "enable", "--now", "julianverse-cloud.timer"])
        run(user_env + ["systemctl", "--user", "start", "--no-block", "julianverse-cloud.service"])
    except (Exception, KeyboardInterrupt):
        print(
            "SSO-Einrichtung fehlgeschlagen. Stelle die vorherige Login-Konfiguration wieder her.",
            flush=True,
        )
        php(
            "$p=json_decode(stream_get_contents(STDIN),true); $c=\\OC::$server->getConfig(); "
            "if ($p===null) {$c->deleteSystemValue('openid-connect');} else {$c->setSystemValue('openid-connect',$p);} "
            "echo json_encode(['ok'=>true]);",
            snapshot["oidc"],
            step="Vorherige ownCloud-Login-Konfiguration wiederherstellen",
        )
        if not snapshot["appEnabled"]:
            run(docker + ["occ", "app:disable", "openidconnect"])
        shutil.copy2(backup / "account.env", ROOT / ".env")
        os.chown(ROOT / ".env", account.pw_uid, account.pw_gid)
        run(user_env + ["systemctl", "--user", "restart", "julianverse-account.service"])
        print(
            "Vorherige Login-Konfiguration wiederhergestellt. Sicherung: " + str(backup), flush=True
        )
        raise
    print("Fertig: ownCloud bietet die Anmeldung mit Julianverse Account an.")
    print(
        "Neue bestätigte Konten erhalten automatisch 1 GB. Vorhandene Konten unter Verbindungen verknüpfen."
    )
    print("Der bisherige ownCloud-Login bleibt verfügbar. Sync bleibt ausgeschaltet.")
    print("Sicherung:", backup)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ownCloud mit Julianverse Account verbinden.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Nur Container und Konsolenkonfiguration prüfen; nichts einrichten.",
    )
    options = parser.parse_args()
    try:
        main(check_only=options.check)
    except KeyboardInterrupt:
        print(
            "Abgebrochen. Die SSO-Einrichtung ist nicht als erfolgreich bestätigt; beim nächsten Aufruf wird der Zustand erneut geprüft.",
            file=sys.stderr,
        )
        sys.exit(130)
    except Exception as error:
        if isinstance(error, RuntimeError):
            print("FEHLER:", error, file=sys.stderr)
        else:
            print(
                "FEHLER: Einrichtung konnte nicht abgeschlossen werden. Zugangsdaten wurden nicht ausgegeben.",
                file=sys.stderr,
            )
        sys.exit(1)
