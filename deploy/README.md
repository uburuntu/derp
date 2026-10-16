# Deployment

Derp runs as one Docker container (`derp`) on the host, polling Telegram, with
the host's PostgreSQL. Releases come only from `main`, and only while the
repository variable `DEPLOY_ENABLED` is `true`.

## How a release works

1. `.github/workflows/deploy.yml` runs CI, then `tools/deployment/build_release.py`
   builds `derp:<sha>` and a private image archive (no registry).
2. `tools/deployment/client.py` streams one JSON line plus the archive over SSH
   to the deploy key's forced command, `~/derp-deploy/deploy.py`
   (`tools/deployment/host.py`). Runtime configuration never travels: it is
   `~/derp-deploy/derp.env` on the host, and each release keeps a private copy.
3. The host verifies the archive (checksum, tag, revision label, runtime
   contract, no configuration baked in), loads it, runs `python -m
   derp.ops.preflight`, then `alembic upgrade head`. Migrations are expand-only,
   so the running release keeps working on the new schema.
4. It stops the current poller (the legacy `derp-bot` container on the first
   release), starts the new one, and waits for the heartbeat healthcheck. Any
   failure restores the previous poller; the outcome lands in
   `~/derp-deploy/releases/<release>/failure.log`, never in public logs.

`.github/workflows/rollback.yml` swaps back to the previous release with the
configuration it was released with.

## One-time host setup

1. Database: `sudo -u postgres pg_create_db derp_v2` (see `postgres/`), and
   make sure the backup timers cover it.
2. `mkdir -m 700 ~/derp-deploy`, copy `tools/deployment/host.py` to
   `~/derp-deploy/deploy.py`, and write `~/derp-deploy/derp.env` (mode 600) from
   `env.example` with `ENVIRONMENT=prod`.
3. Add the deploy key to `~/.ssh/authorized_keys` with the forced command:
   `command="/usr/bin/python3 /home/<user>/derp-deploy/deploy.py",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA… derp-deploy`
4. GitHub `production` environment secrets: `SSH_HOST`, `SSH_USER`, `SSH_PORT`,
   `SSH_KNOWN_HOSTS`, `DEPLOY_SSH_KEY`; then set `DEPLOY_ENABLED=true`.

## Cutover from the previous Derp

1. Stop the old bot so its data stops changing: `docker update --restart=no derp-bot && docker stop derp-bot`.
2. Import: `make import-main SOURCE=postgresql+asyncpg://…/derp` with the new
   `DATABASE_URL` in the environment (read-only on the old database; rerunnable).
3. Release (merge to `main` or run the Deploy workflow). Until the first release
   succeeds, a failure restarts the old container.
