# Production deployment guide

Derp deploys immutable GHCR image digests through a manually approved GitHub
Actions transaction. A push to `main` runs CI, builds, pushes, and attests the
image, but never changes production.

The full transaction, failure inspection, fix-forward procedure, and
compatibility-bound rollback are documented in
[`docs/deployment.md`](../docs/deployment.md). This file covers host setup and
the release entry point.

## Host prerequisites

- Linux/AMD64 with Docker and GNU `timeout`
- PostgreSQL 14 or newer on a private network
- encrypted database backups with a tested restore path
- `/opt/derp/.env.prod`, readable by Docker and not stored in the image
- `/opt/derp/artifacts`, owned by UID/GID `1000:1000` with mode `0700`
- enough disk space to retain the active, previous, and failed-candidate images

Use the scripts under `deploy/postgres/` only for a self-managed PostgreSQL
installation. Do not combine a PostgreSQL major upgrade with an application
release.

```sh
scp -r deploy/postgres user@server:/tmp/postgres-setup
ssh user@server
cd /tmp/postgres-setup
chmod +x *.sh
sudo ./install.sh
sudo -u postgres pg_create_db derp
```

The generic host bootstrap is available as `deploy/setup-server.sh`. Review it
for the target distribution before running it.

## Production environment

Start from `env.example`. At minimum, production requires:

```dotenv
ENVIRONMENT=prod
TELEGRAM_BOT_TOKEN=
BOT_USERNAME=DerpRobot
DATABASE_URL=postgresql+asyncpg://user:password@database:5432/derp

OPENROUTER_API_KEY=
OPENROUTER_APP_TITLE=Derp
OPENROUTER_APP_URL=https://t.me/DerpRobot
OPENROUTER_ENABLED_FEATURES=["chat","inline_chat","image_generate","image_edit"]
GOOGLE_API_PAID_KEY=

LOGFIRE_TOKEN=
LOGFIRE_CAPTURE_AI_CONTENT=false
OPERATOR_IDS=[123456789]
PUBLIC_PURCHASES_ENABLED=false
ARTIFACT_STORE_PATH=/var/lib/derp/artifacts
```

Keep purchases disabled for the initial candidate deployment. Production must
never enable AI-content export. Free-model consent is handled in Telegram;
there is no deployment switch that bypasses it.

## GitHub configuration

Create these Actions secrets:

| Secret | Purpose |
| --- | --- |
| `SSH_HOST` | Production host |
| `SSH_USER` | Restricted deployment user |
| `SSH_PRIVATE_KEY` | Deployment SSH private key |
| `DEPLOY_GHCR_TOKEN` | Fine-grained token with package-read access |

Create a GitHub environment named `production` and require the owner as a
reviewer. Protect `main` with required PR checks. Enable secret scanning and
push protection.

The deployment user must access Docker without world-writable socket
permissions. Add the user to the intended Docker group; never use
`chmod 666 /var/run/docker.sock`.

## Build and deploy

1. Merge the reviewed PR to `main` and wait for the push-triggered **CD** run.
   It performs `build_only` and produces the candidate image and attestation.
2. Take a complete backup and verify its checksum and restoreability. Record a
   non-secret snapshot or backup reference.
3. Open **Actions -> CD -> Run workflow**, select `main`, and enter:
   - `mode=deploy`
   - `expected_sha=<full 40-character main SHA>`
   - `backup_reference=<completed restorable backup reference>`
4. At the `production` environment gate, independently verify the SHA, backup,
   candidate checks, and expected image before approving.
5. CD pulls the image by digest, runs read-only release preflight, stops and
   retains the old poller, migrates, runs read-only verification, then requires
   a healthy restart-free candidate for 60 seconds.

For a validation build without host access, dispatch `mode=build_only` with the
selected full SHA. `latest` is never a deployment identity.

## Release activation

After the candidate is healthy, leave `PUBLIC_PURCHASES_ENABLED=false` while
the operator completes the Telegram and Logfire smoke matrix. The mandatory
commerce check is one real operator-only 1-Star checkout followed by **Refund
latest 1-Star test**. Confirm exactly-once fulfillment, Telegram refund
acceptance, the normal clawback update, and unchanged balances on replay.

Only after the release checklist passes:

1. set `PUBLIC_PURCHASES_ENABLED=true` in `/opt/derp/.env.prod`;
2. redeploy the same `main` SHA through the approval gate;
3. use the Telegram operator console to sync command scopes;
4. verify that public invoices show product version `2026-07-28-v1`;
5. tag the deployed commit `v0.1.0` and record its image digest and Alembic head.

See [`docs/release-v0.1.0.md`](../docs/release-v0.1.0.md) for the complete
acceptance matrix and no-go conditions.

## Basic inspection

```sh
docker ps -a --filter 'name=derp-bot'
docker logs --tail 200 derp-bot
docker inspect derp-bot
docker stats --no-stream derp-bot
```

Do not restart a previous image after a forward migration unless its schema
compatibility check passes. Follow `docs/deployment.md` for every failed-release
or rollback decision.
