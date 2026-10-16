"""Build the release image and a private archive for direct SSH delivery (no registry).

Writes ``image.tar.gz`` and ``metadata.json`` into a fresh private directory under
``RUNNER_TEMP`` and reports the archive path through ``GITHUB_OUTPUT``. The
archive is validated with the same checks the host runs before loading it.
"""

import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location("derp_deploy_host", Path(__file__).with_name("host.py"))
if _spec is None or _spec.loader is None:
    raise SystemExit("host.py is missing")
host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)


def run(*args: str, capture: bool = False) -> str:
    environment = {**os.environ, "DOCKER_BUILDKIT": "1"}  # the Dockerfile uses cache and bind mounts
    return subprocess.run(args, check=True, capture_output=capture, text=True, env=environment).stdout


def archive_image_id(path: Path) -> str:
    """The image ID is the digest of its config (Docker's containerd store may report an index ID)."""
    with tarfile.open(path, "r:gz") as archive:

        def read(name: str) -> bytes:
            member = archive.getmember(name)
            source = archive.extractfile(member)
            if not member.isfile() or member.size > 1024 * 1024 or source is None:
                raise ValueError("Invalid image archive metadata")
            return source.read()

        manifests = json.loads(read("manifest.json"))
        if not isinstance(manifests, list) or len(manifests) != 1:
            raise ValueError("Archive must contain exactly one image")
        return "sha256:" + hashlib.sha256(read(manifests[0]["Config"])).hexdigest()


def main() -> None:
    revision = os.environ["GITHUB_SHA"]
    image = host.TAG + revision
    run(
        "docker", "build", "--platform", "linux/amd64",
        "--label", "org.opencontainers.image.revision=" + revision, "--tag", image, ".",
    )  # fmt: skip
    # The image must import and pass its own healthcheck module without configuration or network.
    run("docker", "run", "--rm", "--network", "none", "--read-only", image, "python", "-c", "import derp.app")
    directory = Path(tempfile.mkdtemp(prefix="derp-release-", dir=os.environ["RUNNER_TEMP"]))
    archive = directory / "image.tar.gz"
    os.umask(0o077)
    with (
        archive.open("wb") as destination,
        gzip.GzipFile(filename="", mode="wb", fileobj=destination, compresslevel=1, mtime=0) as compressed,
    ):
        process = subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE)
        if process.stdout is None:
            raise SystemExit("Image export failed")
        try:
            shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
        finally:
            process.stdout.close()
        if process.wait():
            raise SystemExit("Image export failed")
    checksum = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    metadata = {
        "image": archive_image_id(archive),
        "revision": revision,
        "archive_sha256": checksum.hexdigest(),
        "archive_size": archive.stat().st_size,
    }
    host.validate_archive(archive, metadata)
    (directory / "metadata.json").write_text(json.dumps(metadata))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"archive={archive}\n")
    print("Validated private image archive: " + metadata["image"])


if __name__ == "__main__":
    main()
