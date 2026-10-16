"""Send a release (or a rollback) to the host's forced-command wrapper over verified SSH.

Only the image metadata and archive travel: runtime configuration stays on the
host. Only known status lines are echoed, since SSH diagnostics can expose host
details in public Actions logs.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location("derp_deploy_host", Path(__file__).with_name("host.py"))
if _spec is None or _spec.loader is None:
    raise SystemExit("host.py is missing")
host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)


def public_lines(stdout: str) -> list[str]:
    deployed = re.compile(r"Deployed [0-9a-f]{40} sha256:[0-9a-f]{64}")
    return [line for line in stdout.splitlines() if line in host.PUBLIC_STATUS or deployed.fullmatch(line)]


def main() -> None:
    target = os.environ["DEPLOY_HOST"]
    user = os.environ["DEPLOY_USER"]
    port = int(os.environ.get("DEPLOY_PORT") or "22")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", target)
        or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user)
        or not 1 <= port <= 65535
    ):
        raise SystemExit("Invalid SSH connection settings")
    operation = os.environ.get("DEPLOY_OPERATION", "deploy")
    payload: dict[str, object] = {"action": operation}
    archive: Path | None = None
    if operation == "deploy":
        archive = Path(os.environ["DEPLOY_ARCHIVE"])
        metadata = json.loads(archive.with_name("metadata.json").read_text())
        if metadata["revision"] != os.environ["GITHUB_SHA"]:
            raise SystemExit("Image archive does not match the deployment revision")
        payload.update(metadata)
    host.validate_payload(payload)
    with tempfile.TemporaryDirectory(prefix="derp-ssh-") as scratch:
        directory = Path(scratch)
        key = directory / "key"
        key.write_text(os.environ["DEPLOY_SSH_KEY"].strip() + "\n")
        key.chmod(0o600)
        known_hosts = directory / "known_hosts"
        known_hosts.write_text(os.environ["DEPLOY_KNOWN_HOSTS"].strip() + "\n")
        command = [
            "ssh", "-T", "-i", str(key), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + str(known_hosts), "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            f"{user}@{target}", "derp",
        ]  # fmt: skip
        print("Connecting to deployment host", flush=True)
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=stdout, stderr=subprocess.DEVNULL
            )
            if process.stdin is None:
                raise SystemExit("SSH did not start")
            try:
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                if archive is not None:
                    with archive.open("rb") as source:
                        shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
                process.stdin.close()
            except BrokenPipeError:
                pass
            code = process.wait()
            stdout.seek(0)
            output = stdout.read().decode(errors="replace")
    for line in public_lines(output):
        print(line)
    if code:
        print("Deployment failed; inspect the host privately for details", file=sys.stderr)
    raise SystemExit(code)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, OSError, host.DeploymentError):
        print("Deployment connection configuration is incomplete or invalid", file=sys.stderr)
        raise SystemExit(1) from None
