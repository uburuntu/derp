#!/usr/bin/env python3
"""Restricted SSH entrypoint: its only authority is releasing or rolling back Derp.

Install as ``~/derp-deploy/deploy.py`` and make it the SSH forced command for the
deploy key. It never evaluates SSH_ORIGINAL_COMMAND, shell text or host paths
from the request. Runs on the host's system Python (3.10+), standard library only.

A request is one JSON line, followed by the image archive for a deploy. Runtime
configuration never travels with it: it lives in ``~/derp-deploy/derp.env``
(owner-maintained, 0600) and each release keeps a private copy, so a rollback
restarts the previous image with the configuration it was released with.

Deploy: receive and verify the archive → load the image → preflight →
``alembic upgrade head`` (expand-only migrations, so the running release keeps
working) → stop the current poller → start the new one → wait for the
heartbeat healthcheck → on any failure, restore the previous poller.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
RELEASE_RE = re.compile(r"[0-9a-f]{40}-[0-9]+")
CONTAINER = "derp"
LEGACY = "derp-bot"  # the previous Derp, stopped at the first cutover and restorable until a release succeeds
TAG = "derp:"
USER = "app"
COMMAND = ["python", "-m", "derp"]
MAX_ARCHIVE_SIZE = 2 * 1024**3
MAX_CONFIGURATION_SIZE = 64 * 1024
FORBIDDEN_IMAGE_ENV = (
    "TELEGRAM_",
    "DATABASE_URL",
    "OPENROUTER_",
    "LOGFIRE_TOKEN",
    "WOLFRAM_",
    "JDOODLE_",
    "ACRCLOUD_",
    "OWNER_IDS",
    "GH_TOKEN=",
    "GITHUB_TOKEN=",
)
HEALTH_DEADLINE = 300
PUBLIC_STATUS = (
    "Image received; running preflight",
    "Preflight passed; migrating the database",
    "Database migrated; stopping the current poller",
    "Release failed; restoring the previous poller",
    "Previous poller restored",
    "Rollback completed",
    "Rollback failed; restoring the current release",
)


class DeploymentError(Exception):
    pass


def report(message):
    with contextlib.suppress(OSError):  # a disconnected CI runner must not interrupt cutover or rollback
        print(message, flush=True)


def validate_payload(payload):
    if not isinstance(payload, dict) or payload.get("action") not in {"deploy", "rollback"}:
        raise DeploymentError("Unknown operation")
    if payload["action"] == "rollback":
        if set(payload) != {"action"}:
            raise DeploymentError("Unexpected rollback fields")
        return
    if set(payload) != {"action", "image", "revision", "archive_sha256", "archive_size"}:
        raise DeploymentError("Unexpected deployment fields")
    if not isinstance(payload["image"], str) or not IMAGE_RE.fullmatch(payload["image"]):
        raise DeploymentError("Image must be an immutable SHA256 image ID")
    if not isinstance(payload["revision"], str) or not REVISION_RE.fullmatch(payload["revision"]):
        raise DeploymentError("Invalid source revision")
    if not isinstance(payload["archive_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", payload["archive_sha256"]
    ):
        raise DeploymentError("Invalid archive checksum")
    if type(payload["archive_size"]) is not int or not 0 < payload["archive_size"] <= MAX_ARCHIVE_SIZE:
        raise DeploymentError("Invalid archive size")


def validate_archive(path, state):
    """The archive holds exactly this revision's image, built to Derp's runtime contract."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != state["archive_sha256"]:
        raise DeploymentError("Image archive checksum mismatch")
    with tarfile.open(path, "r:gz") as archive:
        members, total = {}, 0
        for member in archive:
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or not (member.isfile() or member.isdir())
                or member.name in members
            ):
                raise DeploymentError("Unsafe image archive entry")
            total += member.size
            if total > 8 * 1024**3 or len(members) >= 4096:
                raise DeploymentError("Expanded image archive is too large")
            members[member.name] = member

        def read_json(name):
            member = members.get(name)
            if member is None or not member.isfile() or member.size > 1024 * 1024:
                raise DeploymentError("Invalid image archive metadata")
            source = archive.extractfile(member)
            if source is None:
                raise DeploymentError("Invalid image archive metadata")
            data = source.read()
            return data, json.loads(data)

        _, manifests = read_json("manifest.json")
        if not isinstance(manifests, list) or len(manifests) != 1 or not isinstance(manifests[0], dict):
            raise DeploymentError("Archive must contain exactly one image")
        manifest = manifests[0]
        tag = TAG + state["revision"]
        if manifest.get("RepoTags") not in ([tag], ["docker.io/library/" + tag]):
            raise DeploymentError("Archive contains a foreign image tag")
        if not isinstance(manifest.get("Config"), str):
            raise DeploymentError("Invalid image archive configuration")
        raw, config = read_json(manifest["Config"])
        if "sha256:" + hashlib.sha256(raw).hexdigest() != state["image"]:
            raise DeploymentError("Image ID does not match its configuration")
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise DeploymentError("Unsupported image platform")
        runtime = config.get("config", {})
        if (runtime.get("Labels") or {}).get("org.opencontainers.image.revision") != state["revision"]:
            raise DeploymentError("Image source revision mismatch")
        if runtime.get("User") != USER or runtime.get("Cmd") != COMMAND or not runtime.get("Healthcheck"):
            raise DeploymentError("Image does not match Derp's runtime contract")
        if any(value.startswith(FORBIDDEN_IMAGE_ENV) for value in runtime.get("Env") or []):
            raise DeploymentError("Image contains runtime configuration")
        layers = manifest.get("Layers")
        if not isinstance(layers, list) or any(
            layer not in members or not members[layer].isfile() for layer in layers
        ):
            raise DeploymentError("Invalid image layers")


def run_arguments(image, env_path, *, name=CONTAINER):
    """``docker run`` for a long-lived, locked-down poller."""
    return [
        "docker", "run", "--detach", "--name", name,
        "--restart", "unless-stopped", "--init",
        "--env-file", str(env_path),
        "--read-only", "--tmpfs", "/tmp:mode=1777,size=1g",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--memory", "3g", "--memory-swap", "3g", "--cpus", "2", "--pids-limit", "256",
        "--stop-timeout", "60",
        "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        image,
    ]  # fmt: skip


def task_arguments(image, env_path, *command):
    """A one-shot task (preflight, migrations) from the release image and configuration."""
    return [
        "docker", "run", "--rm", "--env-file", str(env_path),
        "--read-only", "--tmpfs", "/tmp:mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        image, *command,
    ]  # fmt: skip


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        f.write(data)
        os.fchmod(f.fileno(), 0o600)
        f.flush()
        os.fsync(f.fileno())
        staging = Path(f.name)
    staging.replace(path)
    sync_directory(path.parent)


def read_private(path):
    """A bounded regular file owned by this account and closed to others; symlinks are refused."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > MAX_CONFIGURATION_SIZE
        ):
            raise DeploymentError("Unsafe private configuration file")
        return source.read(MAX_CONFIGURATION_SIZE + 1).decode("utf-8")


def validate_env_file(text):
    keys = set()
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, _ = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise DeploymentError("Invalid runtime configuration")
        keys.add(key)
    missing = {"ENVIRONMENT", "TELEGRAM_BOT_TOKEN", "DATABASE_URL", "OPENROUTER_API_KEY", "OWNER_IDS"} - keys
    if missing:
        raise DeploymentError("Runtime configuration is missing core settings")


class Deployer:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.diagnostic_path = None

    def run(self, *args, timeout=180, check=True):
        try:
            result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DeploymentError("Operation timed out") from None
        if check and result.returncode:
            # Docker output can quote configuration: it stays in the private release directory.
            if self.diagnostic_path:
                write_private(self.diagnostic_path, (result.stdout + result.stderr)[-131072:])
            raise DeploymentError("Docker operation failed")
        return result.stdout if result.returncode == 0 else None

    def inspect(self, name):
        data = self.run("docker", "inspect", name, check=False)
        return json.loads(data)[0] if data else None

    def read_state(self, filename):
        path = self.root / filename
        return json.loads(path.read_text()) if path.exists() else None

    def release_dir(self, state):
        if not RELEASE_RE.fullmatch(state.get("release", "")):
            raise DeploymentError("Invalid stored release")
        return self.root / "releases" / state["release"]

    def receive_image(self, payload, directory, stream):
        archive = directory / "image.tar.gz"
        remaining = payload["archive_size"]
        if shutil.disk_usage(directory).free < remaining + 2 * 1024**3:
            raise DeploymentError("Insufficient space for image transfer")
        try:
            with archive.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DeploymentError("Image transfer was interrupted")
                    output.write(chunk)
                    remaining -= len(chunk)
            validate_archive(archive, payload)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        self.run("docker", "image", "load", "--input", str(archive), timeout=600)
        if not self.run("docker", "image", "inspect", payload["image"], check=False):
            raise DeploymentError("Transferred image was not loaded")

    def ensure_image(self, state):
        if self.run("docker", "image", "inspect", state["image"], check=False):
            return
        archive = self.release_dir(state) / "image.tar.gz"
        validate_archive(archive, state)
        self.run("docker", "image", "load", "--input", str(archive), timeout=600)

    def record_container_failure(self):
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", CONTAINER],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if self.diagnostic_path:
            write_private(self.diagnostic_path, (result.stdout + result.stderr)[-131072:])

    def stop_current(self):
        if self.inspect(CONTAINER):
            self.run("docker", "stop", "--time", "60", CONTAINER, timeout=90)
            self.run("docker", "rm", CONTAINER)

    def stop_legacy(self):
        info = self.inspect(LEGACY)
        if not info or not info["State"]["Running"]:
            return
        self.run("docker", "update", "--restart=no", LEGACY)
        self.run("docker", "stop", "--time", "30", LEGACY, timeout=60)

    def wait_healthy(self, deadline=HEALTH_DEADLINE, interval=3.0):
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            info = self.inspect(CONTAINER)
            if info:
                state = info["State"]
                health = (state.get("Health") or {}).get("Status")
                if health == "healthy":
                    if info.get("RestartCount", 0):
                        raise DeploymentError("Replacement restarted during startup")
                    return
                if not state["Running"] or health == "unhealthy":
                    break
            time.sleep(interval)
        raise DeploymentError("Replacement did not become ready")

    def start(self, state):
        self.run(*run_arguments(state["image"], self.release_dir(state) / "derp.env"))

    def restore(self, previous):
        """Bring back what ran before: an earlier release, the legacy container, or nothing."""
        if not previous.get("empty") and not previous.get("legacy"):
            self.ensure_image(previous)
        self.stop_current()
        if previous.get("empty"):
            return
        if previous.get("legacy"):
            self.run("docker", "update", "--restart=" + previous["restart_policy"], LEGACY)
            self.run("docker", "start", LEGACY)
            info = self.inspect(LEGACY)
            if not info or not info["State"]["Running"]:
                raise DeploymentError("Legacy container did not restart")
            return
        self.stop_legacy()
        self.start(previous)
        self.wait_healthy()

    def rollback(self):
        previous, current = self.read_state("previous.json"), self.read_state("current.json")
        if not previous or previous.get("empty") or not current:
            raise DeploymentError("No prior release recorded")
        try:
            self.restore(previous)
        except Exception:  # noqa: BLE001 - whatever failed, the current release comes back
            report("Rollback failed; restoring the current release")
            self.restore(current)
            raise DeploymentError("Rollback failed; current release restored") from None
        write_private(self.root / "current.json", json.dumps(previous))
        write_private(self.root / "previous.json", json.dumps(current))
        report("Rollback completed")

    def deploy(self, payload, stream=None):
        validate_payload(payload)
        if payload["action"] == "rollback":
            self.rollback()
            return
        configuration = read_private(self.root / "derp.env")
        validate_env_file(configuration)
        release = payload["revision"] + "-" + str(time.time_ns())
        directory = self.root / "releases" / release
        directory.mkdir(mode=0o700, parents=True)
        self.diagnostic_path = directory / "failure.log"
        write_private(directory / "derp.env", configuration)
        state = {
            "release": release,
            **{k: payload[k] for k in ("revision", "image", "archive_sha256", "archive_size")},
        }
        write_private(directory / "release.json", json.dumps(state))
        self.receive_image(payload, directory, stream)
        report("Image received; running preflight")
        env = directory / "derp.env"
        self.run(*task_arguments(state["image"], env, "python", "-m", "derp.ops.preflight"), timeout=180)
        report("Preflight passed; migrating the database")
        self.run(*task_arguments(state["image"], env, "alembic", "upgrade", "head"), timeout=600)
        report("Database migrated; stopping the current poller")
        previous = self.read_state("current.json")
        if previous is None:
            legacy = self.inspect(LEGACY)
            previous = (
                {"legacy": True, "restart_policy": legacy["HostConfig"]["RestartPolicy"]["Name"] or "no"}
                if legacy
                else {"empty": True}
            )
        try:
            self.stop_legacy()  # never two pollers on one token
            self.stop_current()
            self.start(state)
            self.wait_healthy()
        except Exception:  # noqa: BLE001 - whatever failed, the previous poller comes back
            with contextlib.suppress(Exception):  # diagnostics must not block the restore
                self.record_container_failure()
            report("Release failed; restoring the previous poller")
            self.restore(previous)
            report("Previous poller restored")
            raise DeploymentError("Release failed and was rolled back") from None
        write_private(self.root / "previous.json", json.dumps(previous))
        write_private(self.root / "current.json", json.dumps(state))
        report("Deployed " + payload["revision"] + " " + payload["image"])
        with contextlib.suppress(Exception):  # cleanup must not fail a healthy release
            self.prune_releases()

    def prune_releases(self):
        keep = [self.read_state(name) for name in ("current.json", "previous.json")]
        directories = {state["release"] for state in keep if state and "release" in state}
        images = {state["image"] for state in keep if state and "image" in state}
        for directory in (self.root / "releases").iterdir():
            if (
                directory.name in directories
                or directory.is_symlink()
                or not RELEASE_RE.fullmatch(directory.name)
            ):
                continue
            metadata = directory / "release.json"
            if metadata.exists():
                old = json.loads(metadata.read_text())
                if IMAGE_RE.fullmatch(old.get("image", "")) and old["image"] not in images:
                    self.run("docker", "image", "rm", old["image"], check=False)
            shutil.rmtree(directory)


def main():
    os.umask(0o077)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    root = Path(__file__).resolve().parent
    data = sys.stdin.buffer.readline(4097)
    if len(data) > 4096:
        raise DeploymentError("Deployment request is too large")
    try:
        payload = json.loads(data)
    except ValueError:
        raise DeploymentError("Invalid deployment request") from None
    validate_payload(payload)
    with (root / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Deployer(root).deploy(payload, sys.stdin.buffer)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - only our own messages reach the public log
        print(
            str(error)
            if isinstance(error, DeploymentError)
            else "Deployment failed; inspect the host privately",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
