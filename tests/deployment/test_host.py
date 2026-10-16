"""The host's release wrapper, driven with a synthetic archive and a scripted Docker."""

import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("derp_deploy_host", ROOT / "tools" / "deployment" / "host.py")
assert _spec is not None and _spec.loader is not None
host: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)

REVISION = "a" * 40
ENV = "ENVIRONMENT=prod\nTELEGRAM_BOT_TOKEN=1:x\nDATABASE_URL=postgresql+asyncpg://u:p@h/d\nOPENROUTER_API_KEY=k\nOWNER_IDS=[1]\n"


def config(**overrides: Any) -> dict[str, Any]:
    runtime = {
        "User": "app",
        "Cmd": ["python", "-m", "derp"],
        "Healthcheck": {"Test": ["CMD", "python", "-m", "derp.telegram.heartbeat"]},
        "Labels": {"org.opencontainers.image.revision": REVISION},
        "Env": ["PATH=/app/.venv/bin", "HEARTBEAT_PATH=/tmp/derp.poll"],
    }
    runtime.update(overrides)
    return {"os": "linux", "architecture": "amd64", "config": runtime}


def make_archive(path: Path, image_config: dict[str, Any], tag: str = "derp:" + REVISION) -> dict[str, Any]:
    raw = json.dumps(image_config).encode()
    digest = hashlib.sha256(raw).hexdigest()
    files = {
        "manifest.json": json.dumps(
            [{"Config": f"{digest}.json", "RepoTags": [tag], "Layers": ["layer.tar"]}]
        ).encode(),
        f"{digest}.json": raw,
        "layer.tar": b"layer",
    }
    with (
        path.open("wb") as out,
        gzip.GzipFile(fileobj=out, mode="wb") as zipped,
        tarfile.open(fileobj=zipped, mode="w") as tar,
    ):
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = path.read_bytes()
    return {
        "action": "deploy",
        "image": "sha256:" + digest,
        "revision": REVISION,
        "archive_sha256": hashlib.sha256(blob).hexdigest(),
        "archive_size": len(blob),
    }


def test_payloads_are_strict() -> None:
    host.validate_payload({"action": "rollback"})
    with pytest.raises(host.DeploymentError):
        host.validate_payload({"action": "rollback", "image": "x"})
    with pytest.raises(host.DeploymentError):
        host.validate_payload({"action": "shell", "cmd": "rm -rf /"})
    good = {
        "action": "deploy",
        "image": "sha256:" + "b" * 64,
        "revision": REVISION,
        "archive_sha256": "c" * 64,
        "archive_size": 10,
    }
    host.validate_payload(good)
    for key, bad in (
        ("image", "derp:latest"),
        ("revision", "main"),
        ("archive_size", 0),
        ("archive_size", True),
    ):
        with pytest.raises(host.DeploymentError):
            host.validate_payload({**good, key: bad})
    with pytest.raises(host.DeploymentError):
        host.validate_payload({**good, "environment": {"X": "y"}})  # configuration never travels


def test_archives_must_match_the_runtime_contract(tmp_path: Path) -> None:
    archive = tmp_path / "image.tar.gz"
    host.validate_archive(archive, make_archive(archive, config()))
    cases = [
        (config(Env=["TELEGRAM_BOT_TOKEN=1:x"]), "derp:" + REVISION, "runtime configuration"),
        (config(User="root"), "derp:" + REVISION, "runtime contract"),
        (config(Healthcheck=None), "derp:" + REVISION, "runtime contract"),
        (config(Labels={}), "derp:" + REVISION, "revision mismatch"),
        (config(), "derp:" + "f" * 40, "foreign image tag"),
    ]
    for image_config, tag, message in cases:
        state = make_archive(archive, image_config, tag)
        with pytest.raises(host.DeploymentError, match=message):
            host.validate_archive(archive, state)
    state = make_archive(archive, config())
    with pytest.raises(host.DeploymentError, match="checksum"):
        host.validate_archive(archive, {**state, "archive_sha256": "0" * 64})


def test_env_files_need_the_core_settings() -> None:
    host.validate_env_file(ENV + "# comment\n\nLOGFIRE_TOKEN=t\n")
    with pytest.raises(host.DeploymentError, match="core settings"):
        host.validate_env_file("ENVIRONMENT=prod\n")
    with pytest.raises(host.DeploymentError, match="Invalid"):
        host.validate_env_file(ENV + "export X=1\n")


def test_the_poller_runs_locked_down() -> None:
    args = host.run_arguments("sha256:" + "b" * 64, Path("/r/derp.env"))
    joined = " ".join(args)
    assert "--read-only" in args and "--cap-drop ALL" in joined and "no-new-privileges:true" in args
    assert "--restart unless-stopped" in joined and args[-1] == "sha256:" + "b" * 64


class FakeDocker(host.Deployer):
    """Containers as a dict; the replacement's health is scripted."""

    def __init__(self, root: Path, *, healthy: bool = True, legacy: bool = False) -> None:
        super().__init__(root)
        self.healthy = healthy
        self.containers: dict[str, dict[str, Any]] = {}
        self.images: set[str] = set()
        self.commands: list[tuple[str, ...]] = []
        if legacy:
            self.containers["derp-bot"] = {"image": "old", "running": True, "restart": "unless-stopped"}

    def run(self, *args: str, timeout: int = 180, check: bool = True) -> str | None:
        self.commands.append(args)
        match args:
            case ("docker", "image", "load", *_):
                self.images.add(self.loading)
            case ("docker", "image", "inspect", image):
                return "[]" if image in self.images else None
            case ("docker", "run", "--detach", "--name", name, *rest):
                self.containers[name] = {"image": rest[-1], "running": True, "restart": "unless-stopped"}
            case ("docker", "update", flag, name):
                self.containers[name]["restart"] = flag.split("=", 1)[1]
            case ("docker", "stop", _, _, name):
                self.containers[name]["running"] = False
            case ("docker", "start", name):
                self.containers[name]["running"] = True
            case ("docker", "rm", name):
                del self.containers[name]
        return ""

    def inspect(self, name: str) -> dict[str, Any] | None:
        box = self.containers.get(name)
        if box is None:
            return None
        health = "healthy" if self.healthy or box["image"] != self.loading else "unhealthy"
        return {
            "State": {"Running": box["running"], "Health": {"Status": health}},
            "RestartCount": 0,
            "HostConfig": {"RestartPolicy": {"Name": box["restart"]}},
        }

    def record_container_failure(self) -> None:
        return None


def release(root: Path, deployer: FakeDocker, payload: dict[str, Any], archive: Path) -> None:
    deployer.loading = payload["image"]
    with archive.open("rb") as stream:
        deployer.deploy(payload, stream)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "derp.env").write_text(ENV)
    (tmp_path / "derp.env").chmod(0o600)
    return tmp_path


def test_first_release_replaces_the_legacy_bot_and_rolls_back(root: Path, tmp_path: Path) -> None:
    archive = tmp_path / "image.tar.gz"
    payload = make_archive(archive, config())
    docker = FakeDocker(root, legacy=True)
    release(root, docker, payload, archive)
    assert docker.containers["derp"]["running"] and not docker.containers["derp-bot"]["running"]
    assert docker.containers["derp-bot"]["restart"] == "no"
    tasks = [c for c in docker.commands if c[:3] == ("docker", "run", "--rm")]
    assert [c[-3:] for c in tasks] == [("python", "-m", "derp.ops.preflight"), ("alembic", "upgrade", "head")]
    current = json.loads((root / "current.json").read_text())
    assert current["image"] == payload["image"] and json.loads((root / "previous.json").read_text())["legacy"]
    release_env = root / "releases" / current["release"] / "derp.env"
    assert release_env.read_text() == ENV and release_env.stat().st_mode & 0o077 == 0

    docker.rollback()  # back to the legacy bot
    assert "derp" not in docker.containers and docker.containers["derp-bot"]["running"]
    assert docker.containers["derp-bot"]["restart"] == "unless-stopped"


def test_an_unhealthy_release_restores_the_previous_one(root: Path, tmp_path: Path) -> None:
    first_archive = tmp_path / "first.tar.gz"
    first = make_archive(first_archive, config())
    docker = FakeDocker(root)
    release(root, docker, first, first_archive)

    second_archive = tmp_path / "second.tar.gz"
    second_config = config(Env=["PATH=/app/.venv/bin", "VERSION=2"])
    second = make_archive(second_archive, second_config)
    docker.healthy = False
    with pytest.raises(host.DeploymentError, match="rolled back"):
        release(root, docker, second, second_archive)
    assert docker.containers["derp"]["image"] == first["image"] and docker.containers["derp"]["running"]
    assert json.loads((root / "current.json").read_text())["image"] == first["image"]


def test_unsafe_configuration_files_are_refused(root: Path, tmp_path: Path) -> None:
    (root / "derp.env").chmod(0o644)
    archive = tmp_path / "image.tar.gz"
    payload = make_archive(archive, config())
    with pytest.raises(host.DeploymentError, match="Unsafe"):
        release(root, FakeDocker(root), payload, archive)
