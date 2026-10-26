"""Installed distribution and isolated local image packaging checks."""

import json
import os
import subprocess
import time
import tomllib
import urllib.request
import uuid
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=30
    )


def test_wheel_contains_console_script_and_admin_template(tmp_path):
    subprocess.run(
        ["uv", "build", "--package", "hoodini", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(tmp_path.glob("hoodini-0.1.0-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        assert "hoodini/cli.py" in names
        for asset in (
            "templates/admin.html",
            "templates/device.html",
            "templates/diagnostic.html",
            "static/admin.css",
            "static/admin.js",
            "static/vendor/pico.min.css",
            "static/vendor/PICO-LICENSE.md",
        ):
            assert f"hoodini/api/{asset}" in names
        scripts = wheel.read("hoodini-0.1.0.dist-info/entry_points.txt")
        assert b"hoodini = hoodini.cli:main" in scripts


def test_compose_configuration():
    if not shutil_which_docker():
        pytest.skip("docker executable unavailable")
    result = docker(
        "compose", "-f", str(ROOT / "compose.yaml"), "config", "--format", "json"
    )
    service = json.loads(result.stdout)["services"]["hoodini"]
    assert service["network_mode"] == "host"
    assert service["restart"] == "unless-stopped"
    # Compose may omit the optional bind key in normalized config. The source
    # declaration still must prevent creation of a missing host directory.
    assert "create_host_path: false" in (ROOT / "compose.yaml").read_text()
    assert any(
        mount["type"] == "bind"
        and mount["source"] == str((ROOT / "data").resolve())
        and mount["target"] == "/data"
        and mount.get("bind", {}).get("create_host_path", False) is False
        for mount in service["volumes"]
    )


def shutil_which_docker():
    from shutil import which

    return which("docker")


def test_docker_build_context_contains_only_wheel_inputs(tmp_path):
    if not shutil_which_docker():
        pytest.skip("docker executable unavailable")
    # Export the actual Docker-filtered context without creating an image. Inspect
    # names only; tmp_path is removed by pytest (no private file content read).
    context = tmp_path / "context"
    context.mkdir()
    result = subprocess.run(
        [
            "docker",
            "build",
            "--progress=quiet",
            "--output",
            f"type=local,dest={context}",
            "-f",
            "-",
            ".",
        ],
        input="FROM scratch\nCOPY . /\n",
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=120,
        check=True,
    )
    assert result.returncode == 0
    paths = {
        p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file()
    }
    assert {
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
        "server/pyproject.toml",
        "server/src/hoodini/cli.py",
    } <= paths
    assert all(
        path
        in {
            "pyproject.toml",
            "uv.lock",
            "LICENSE",
            "server/pyproject.toml",
            "docker-entrypoint.sh",
        }
        or path.startswith("server/src/")
        for path in paths
    )
    assert not any(
        part in {"tests", "scratch", "data", ".git", ".superpowers"}
        for path in paths
        for part in Path(path).parts
    )
    assert not any(
        path.endswith((".key", ".pem", ".p12", ".pfx", ".log")) for path in paths
    )


def test_dockerfile_image_version_matches_package_and_addon():
    import yaml

    dockerfile = (ROOT / "Dockerfile").read_text()
    package = tomllib.loads((ROOT / "server/pyproject.toml").read_text())["project"][
        "version"
    ]
    addon = str(
        yaml.safe_load((ROOT / "addon/hoodini/config.yaml").read_text())["version"]
    )
    assert f'org.opencontainers.image.version="{package}"' in dockerfile
    assert addon == package


def test_image_user_id_is_explicit():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "groupadd --system --gid 999 hoodini" in dockerfile
    assert "useradd --system --uid 999 --gid hoodini" in dockerfile


def test_native_and_image_data_defaults(monkeypatch):
    from hoodini.config import load_settings

    monkeypatch.delenv("HOODINI_DATA_DIR", raising=False)
    assert load_settings(None).data_dir == Path("./data")
    monkeypatch.setenv("HOODINI_DATA_DIR", "/data")
    assert load_settings(None).data_dir == Path("/data")


@pytest.mark.image
@pytest.mark.skipif(
    not os.environ.get("HOODINI_TEST_IMAGE"), reason="HOODINI_TEST_IMAGE unset"
)
def test_image_persistent_private_state_and_isolated_listeners():
    image = os.environ["HOODINI_TEST_IMAGE"]
    unique = uuid.uuid4().hex
    name, volume = f"hoodini-test-{unique}", f"hoodini-test-data-{unique}"
    docker("volume", "create", volume)
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::8765",
            "-v",
            f"{volume}:/data",
            image,
        )
        assert (
            docker("exec", name, "hoodini", "--version").stdout.strip()
            == "hoodini 0.1.0"
        )
        script = (
            "import importlib.util,os,socket,stat;"
            "assert os.geteuid()==999 and os.getegid()==999;"
            "assert all(importlib.util.find_spec(x) is None for x in ('bleak','androguard','yaml'));"
            "assert all(stat.S_IMODE(os.stat('/data/'+x).st_mode)==0o600 "
            "for x in ('server.key','api_token'));"
            "assert stat.S_IMODE(os.stat('/data').st_mode)==0o700;"
            "s=socket.socket();s.settimeout(2);"
            "assert s.connect_ex((socket.gethostbyname(socket.gethostname()),8099))!=0"
        )
        mapping = docker("port", name, "8765/tcp").stdout.strip()
        assert mapping.startswith("127.0.0.1:")
        assert "8099/tcp" not in docker("port", name).stdout
        container = json.loads(docker("inspect", name).stdout)[0]
        assert container["HostConfig"]["Privileged"] is False
        assert not container["HostConfig"]["Devices"]
        url = f"http://127.0.0.1:{mapping.rsplit(':', 1)[1]}/api/v1/devices"
        for _ in range(100):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    assert response.status == 200
                    assert response.read() == b"[]"
                break
            except (OSError, ConnectionError):
                time.sleep(0.1)
        else:
            pytest.fail("LAN API did not start")
        docker("exec", "--user", "999:999", name, "python", "-c", script)
        # Never surface a token in assertion output; only compare captured values.
        cli_token = docker(
            "exec", "-w", "/tmp", name, "hoodini", "token"
        ).stdout.strip()
        server_token = docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path;print(Path('/data/api_token').read_text())",
        ).stdout.strip()
        if cli_token != server_token:
            pytest.fail("image token command does not use server data directory")
        before = docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path;import hashlib;"
            "print(*(hashlib.sha256((Path('/data')/x).read_bytes()).hexdigest() "
            "for x in ('server.crt','server.key','api_token')))",
        ).stdout
        docker("restart", name)
        after = docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path;import hashlib;"
            "print(*(hashlib.sha256((Path('/data')/x).read_bytes()).hexdigest() "
            "for x in ('server.crt','server.key','api_token')))",
        ).stdout
        assert before == after
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume)


# The synthetic Supervisor lives solely on container loopback; the alias is
# test-owned and cannot route the bearer token to an external hostname.
_FAKE_SUPERVISOR = """
import http.server, json, os, socket, subprocess, sys, threading
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.headers.get('Authorization') == 'Bearer synthetic-supervisor-token'
        if self.path == '/addons/self/info':
            data = {'result': 'ok', 'data': {
                'ingress_port': 0 if os.environ.get('FAKE_INVALID') else 9012,
                'ingress_entry': '/api/hassio_ingress/synthetic',
                'ip_address': socket.gethostbyname(socket.gethostname())}}
            status = 200
        elif self.path == '/services/mqtt':
            if os.environ.get('FAKE_MQTT'):
                data, status = {'result': 'ok', 'data': {
                    'host': '127.0.0.1', 'port': 1883, 'ssl': False}}, 200
            else:
                data, status = {'result': 'error', 'message': 'unavailable'}, 503
        else:
            data, status = {'result': 'error'}, 404
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 80), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
sys.exit(subprocess.run(['setpriv', '--reuid=999', '--regid=999', '--clear-groups', 'hoodini', 'serve']).returncode)
"""


@pytest.mark.image
@pytest.mark.skipif(
    not os.environ.get("HOODINI_TEST_IMAGE"), reason="HOODINI_TEST_IMAGE unset"
)
def test_missing_wanted_mqtt_keeps_core_alive():
    image = os.environ["HOODINI_TEST_IMAGE"]
    name = f"hoodini-addon-{uuid.uuid4().hex}"
    volume = f"{name}-data"
    docker("volume", "create", volume)
    try:
        # Seed add-on options; synthetic Supervisor stays root to bind its loopback port.
        # The mock drops privileges before starting Hoodini.
        docker(
            "run",
            "--rm",
            "-v",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            'from pathlib import Path;Path(\'/data/options.json\').write_text(\'{"mqtt": {}, "api": {"port": 8765, "interfaces": [], "addresses": []}, "capture": false, "log_level": "INFO"}\')',
        )
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--add-host",
            "supervisor:127.0.0.1",
            "-e",
            "HOODINI_MODE=addon",
            "-e",
            "SUPERVISOR_TOKEN=synthetic-supervisor-token",
            "-p",
            "127.0.0.1::8765",
            "-v",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            _FAKE_SUPERVISOR,
        )
        mapping = docker("port", name, "8765/tcp").stdout.strip()
        url = f"http://127.0.0.1:{mapping.rsplit(':', 1)[1]}/api/v1/devices"
        for _ in range(100):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    assert response.status == 200 and response.read() == b"[]"
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("add-on LAN API did not start; " + docker("logs", name).stdout)
        script = (
            "import os,socket,stat;"
            "assert os.geteuid()==999;"
            "assert stat.S_IMODE(os.stat('/data/api_token').st_mode)==0o600;"
            "s=socket.socket();s.settimeout(2);"
            "assert s.connect_ex((socket.gethostbyname(socket.gethostname()),9012))==0;"
            "s=socket.socket();s.settimeout(2);"
            "assert s.connect_ex((socket.gethostbyname(socket.gethostname()),8765))==0"
        )
        docker("exec", "--user", "999:999", name, "python", "-c", script)
        container = json.loads(docker("inspect", name).stdout)[0]
        assert (
            not container["HostConfig"]["Privileged"]
            and not container["HostConfig"]["Devices"]
        )
        logs = docker("logs", name).stdout + docker("logs", name).stderr
        assert "restart the add-on after installing/configuring MQTT" in logs
        assert logs.count("Legacy capture option ignored.") == 1
        assert "synthetic-supervisor-token" not in logs
        assert "9012/tcp" not in docker("port", name).stdout
        token_hash = docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path;import hashlib;print(hashlib.sha256(Path('/data/api_token').read_bytes()).hexdigest())",
        ).stdout.strip()
        docker("rm", "-f", name)
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--add-host",
            "supervisor:127.0.0.1",
            "-e",
            "HOODINI_MODE=addon",
            "-e",
            "FAKE_MQTT=1",
            "-e",
            "SUPERVISOR_TOKEN=synthetic-supervisor-token",
            "-v",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            _FAKE_SUPERVISOR,
        )
        for _ in range(100):
            restarted_logs = docker("logs", name).stdout + docker("logs", name).stderr
            if "HA bridge broker unavailable" in restarted_logs:
                break
            time.sleep(0.1)
        else:
            pytest.fail("bridge did not start after service became available")
        assert (
            "restart the add-on after installing/configuring MQTT" not in restarted_logs
        )
        assert "synthetic-supervisor-token" not in restarted_logs
        assert (
            docker(
                "exec",
                name,
                "python",
                "-c",
                "from pathlib import Path;import hashlib;print(hashlib.sha256(Path('/data/api_token').read_bytes()).hexdigest())",
            ).stdout.strip()
            == token_hash
        )
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume)


@pytest.mark.image
@pytest.mark.skipif(
    not os.environ.get("HOODINI_TEST_IMAGE"), reason="HOODINI_TEST_IMAGE unset"
)
def test_invalid_addon_ingress_exits_before_listening():
    image = os.environ["HOODINI_TEST_IMAGE"]
    name = f"hoodini-invalid-{uuid.uuid4().hex}"
    volume = f"{name}-data"
    docker("volume", "create", volume)
    try:
        docker(
            "run",
            "--rm",
            "-v",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            "from pathlib import Path;Path('/data/options.json').write_text('{}')",
        )
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--add-host",
            "supervisor:127.0.0.1",
            "-e",
            "HOODINI_MODE=addon",
            "-e",
            "FAKE_INVALID=1",
            "-e",
            "SUPERVISOR_TOKEN=synthetic-supervisor-token",
            "-v",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            _FAKE_SUPERVISOR,
        )
        result = docker("wait", name)
        assert result.stdout.strip() != "0"
        logs = docker("logs", name).stdout + docker("logs", name).stderr
        assert "Invalid Supervisor ingress context" in logs
        assert "synthetic-supervisor-token" not in logs
        assert "Cannot bind" not in logs
        assert (
            "api_token"
            not in docker(
                "run",
                "--rm",
                "-v",
                f"{volume}:/data",
                "--entrypoint",
                "python",
                image,
                "-c",
                "from pathlib import Path;print(*[p.name for p in Path('/data').iterdir()])",
            ).stdout
        )
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume)


@pytest.mark.image
@pytest.mark.skipif(
    not os.environ.get("HOODINI_TEST_IMAGE"), reason="HOODINI_TEST_IMAGE unset"
)
def test_root_owned_data_dir_starts_hoodini_as_uid_999(tmp_path):
    image = os.environ["HOODINI_TEST_IMAGE"]
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    name = f"hoodini-root-data-{uuid.uuid4().hex}"
    docker(
        "run", "-d", "--name", name, "--network", "none", "-v", f"{data}:/data", image
    )
    try:
        for _ in range(100):
            # Look inside the container: on Linux the unprivileged test process cannot
            # traverse the 999:999 0700 directory (Docker Desktop hides this on macOS).
            probe = docker(
                "exec",
                "--user",
                "0",
                name,
                "sh",
                "-c",
                "awk '/^Uid:/ {print $2}' /proc/1/status; "
                "test -f /data/api_token && echo token",
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.split() == ["999", "token"]:
                break
            time.sleep(0.1)
        else:
            pytest.fail(
                "hoodini did not start as UID 999 with root-owned data directory"
            )
        assert data.stat().st_mode & 0o777 == 0o700
    finally:
        docker("rm", "-f", name, check=False)
        # Hand the directory back so pytest can remove its temporary files.
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0",
            "--entrypoint",
            "chown",
            "-v",
            f"{data}:/data",
            image,
            "-R",
            f"{os.getuid()}:{os.getgid()}",
            "/data",
            check=False,
        )
