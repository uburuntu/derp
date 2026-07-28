# Production deployment and recovery

The CD workflow builds one Linux/AMD64 image, pushes it to GHCR, and attests
the exact manifest digest produced by the build. A push to `main` stops there:
it never deploys. Production deployment is an explicit `workflow_dispatch`
request for the selected `main` commit. Tags such as `latest` are discovery
aids only; they are never deployment identities.

## Release request

Open the **CD** workflow, select the `main` branch, and provide all inputs:

- `mode=build_only` validates, builds, pushes, and attests without contacting
  the production host.
- `mode=deploy` performs the same immutable build and then enters the
  `production` environment approval and deployment transaction.
- `expected_sha` is the full lowercase 40-character SHA displayed for the
  selected `main` commit. The workflow rejects a mismatch before checkout.
- `backup_reference` is required and must be nonblank for `deploy`. Use the
  completed provider snapshot ID or the protected dump/checksum reference.
  The workflow confirms its presence but does not print it into job summaries.

Use `build_only` for candidate evidence. Use `deploy` only after the candidate
commit is merged to `main`, its backup is complete and restorable, and its exact
SHA has been independently checked. The deployed container records both the
commit SHA and immutable image digest as labels and final verification checks
both values.

## Release invariants

- The GitHub `production` environment is the approval boundary. Configure an
  owner approval requirement in repository settings and approve a release only
  after verifying the backup named by `backup_reference`.
- `/opt/derp/.env.prod` must exist on the deployment host and remain readable
  by Docker. It is never copied into the image. Keep
  `ARTIFACT_STORE_PATH=/var/lib/derp/artifacts` in that file; CD also supplies
  the value explicitly so older environment files fail safe.
- Generated artifacts live in the private host directory
  `/opt/derp/artifacts`, mounted at `/var/lib/derp/artifacts`. CD enforces owner
  UID/GID `1000:1000` and mode `0700` before touching the active container.
- The host needs Docker and GNU `timeout`. The workflow bounds registry,
  migration, startup, and readiness commands and fails when either is absent.
- Only one Telegram poller may run. The current container is stopped before
  migration and the candidate is not started until the database is at every
  Alembic head.
- A successful forward migration is not automatically reversible. The
  workflow never starts the previous image after migration begins.
- `python -m derp.release preflight` and `verify` are read-only. Their JSON
  output contains only schema versions, booleans, and aggregate counts; errors
  use a fixed message and never include database URLs, exception text, row
  identifiers, or message content.

After the database connects and Telegram `getMe` succeeds, the runtime writes a
private heartbeat that is refreshed by the application event loop. The Docker
health check requires that heartbeat to remain fresh, and deployment requires a
healthy, restart-free container for 60 seconds. This proves startup,
authentication, and event-loop liveness; it does not claim that a model
provider is reachable.

## Backup gate

Use the database provider's snapshot facility when available. Before starting
`mode=deploy`, record its identifier in `backup_reference` and confirm that the
provider reports it complete and restorable. Recheck that same reference at the
GitHub production approval boundary. Periodically restore a snapshot into an
isolated database; completion alone is not a restore test.

For self-managed PostgreSQL, create a custom-format dump from a trusted admin
host. `LIBPQ_DATABASE_URL` must be a native `postgresql://` URL, not the
application's `postgresql+asyncpg://` SQLAlchemy URL. Prefer a protected
credential file or provider-native authentication over shell history.

```sh
set -eu
umask 077
BACKUP="derp-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --format=custom --file="$BACKUP" "$LIBPQ_DATABASE_URL"
test -s "$BACKUP"
pg_restore --list "$BACKUP" >/dev/null
sha256sum "$BACKUP" >"$BACKUP.sha256"
sha256sum --check "$BACKUP.sha256"
```

Store the dump, checksum, deployed image digest, and current Alembic revision
together outside the application host.

## Automated sequence

1. The request gate requires `main`, matches `expected_sha` to the selected
   commit, validates the mode, and requires backup evidence for deployments.
2. CI passes and the exact commit is built, pushed, and attested. `build_only`
   and every `main` push finish here.
3. After production approval, the host pulls
   `ghcr.io/<owner>/<repo>@sha256:<digest>` and verifies that
   exact reference in Docker's `RepoDigests`.
4. The candidate prepares and verifies the persistent private artifact mount.
5. The candidate runs `python -m derp.release preflight` while the active bot
   is untouched. It requires PostgreSQL 14+, one known Alembic revision, the
   upgradeable legacy schema, and zero negative user/chat balances. It also
   reports `legacy_group_history_purge_count`, the exact aggregate number of
   pre-consent group rows the migration is expected to delete. Record and
   compare this count with the restored-backup rehearsal before continuing.
6. The active `derp-bot` is renamed to `derp-bot-previous` and fully stopped.
7. The candidate runs `alembic upgrade head`, followed by
   `python -m derp.release verify`. Verification requires the image's sole
   Alembic head, the complete core schema, no pending legacy-history purge, and
   no shortfall between positive legacy balances and migrated wallet lots.
8. The exact candidate starts as `derp-bot` with the artifact bind mount. Its
   SHA and digest are stored in Docker labels, and its event-loop heartbeat
   must become healthy during the readiness window.
9. When an active container existed, `derp-bot-previous` remains stopped. The
   next successful deployment rotates it; routine image cleanup is
   intentionally outside the release transaction.

If migration or readiness fails, the workflow exits without starting the old
binary. A candidate that reached startup but failed readiness is retained as
`derp-bot-failed`; migration failures retain the stopped previous container.
The workflow prints candidate logs when available plus all deployment
container states. The immutable reference is recorded in the workflow summary
before the remote deployment starts.

The artifact directory is process-private and survives container replacement
so delivery reconciliation can finish after a restart. It may contain user
media: do not expose it through a web server, share it between environments,
or relax its permissions. Application TTL cleanup remains responsible for
deleting expired files.

## Inspect a failed release

Run these commands on the deployment host:

```sh
docker ps -a --filter 'name=derp-bot'
docker inspect derp-bot-failed 2>/dev/null || true
docker logs --tail 200 derp-bot-failed 2>&1 || true
docker inspect derp-bot-previous 2>/dev/null || true
```

Take the immutable candidate reference from the failed workflow summary, then
inspect the database revision through that image:

```sh
IMAGE='ghcr.io/<owner>/<repo>@sha256:<candidate-digest>'
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  "$IMAGE" python -m derp.release preflight
```

Choose exactly one recovery path below. Do not delete
`derp-bot-previous` or the verified backup until service is restored.

## Fix forward

This is the default after the database reached the candidate's migration head.
Build a corrected image from the compatible code line, then run or re-dispatch
CD with its digest. For urgent manual recovery:

```sh
set -eu
IMAGE='ghcr.io/<owner>/<repo>@sha256:<fixed-digest>'
docker pull "$IMAGE"
docker run --rm \
  --user 0:0 \
  --volume /opt/derp/artifacts:/artifacts \
  "$IMAGE" \
  sh -c 'chown 1000:1000 /artifacts && chmod 0700 /artifacts'
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  "$IMAGE" alembic upgrade head
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  "$IMAGE" python -m derp.release verify
docker rm -f derp-bot derp-bot-failed 2>/dev/null || true
docker run -d \
  --name derp-bot \
  --restart unless-stopped \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  --mount type=bind,source=/opt/derp/artifacts,target=/var/lib/derp/artifacts \
  "$IMAGE"
```

Observe `docker logs --tail 200 derp-bot` and confirm the container remains
running before closing the incident. The next normal CD run restores deployment
labels and digest verification.

## Compatibility rollback

An application-only rollback is allowed only when the previous image accepts
the live schema. Test that invariant using the retained container's local image
ID. Recreate it with the current artifact mount instead of starting the old
container configuration; the first release with persistent artifacts may have
retained a container that predates the mount.

```sh
set -eu
OLD_IMAGE_ID=$(docker inspect --format '{{.Image}}' derp-bot-previous)
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  "$OLD_IMAGE_ID" alembic current --check-heads
docker rm -f derp-bot derp-bot-failed derp-bot-previous 2>/dev/null || true
docker run -d \
  --name derp-bot \
  --restart unless-stopped \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  --mount type=bind,source=/opt/derp/artifacts,target=/var/lib/derp/artifacts \
  "$OLD_IMAGE_ID"
```

If the Alembic check fails, do not start the old application. Either fix
forward or restore the matching pre-deploy database backup and old image as a
pair.

For a self-managed custom-format dump, stop every application process, confirm
the backup checksum, restore it, and verify it with the old image:

```sh
set -eu
docker stop derp-bot derp-bot-failed derp-bot-previous 2>/dev/null || true
OLD_IMAGE_ID=$(docker inspect --format '{{.Image}}' derp-bot-previous)
sha256sum --check /secure/path/derp-backup.dump.sha256
pg_restore \
  --exit-on-error \
  --clean \
  --if-exists \
  --single-transaction \
  --no-owner \
  --no-privileges \
  --dbname "$LIBPQ_DATABASE_URL" \
  /secure/path/derp-backup.dump
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  "$OLD_IMAGE_ID" alembic current --check-heads
docker rm -f derp-bot derp-bot-failed derp-bot-previous 2>/dev/null || true
docker run -d \
  --name derp-bot \
  --restart unless-stopped \
  --add-host host.docker.internal:host-gateway \
  --env-file /opt/derp/.env.prod \
  --env ARTIFACT_STORE_PATH=/var/lib/derp/artifacts \
  --mount type=bind,source=/opt/derp/artifacts,target=/var/lib/derp/artifacts \
  "$OLD_IMAGE_ID"
```

For a managed database, use its point-in-time restore procedure instead of
`pg_restore`, then run the same old-image Alembic check before restarting it.
Never downgrade schema in place unless the exact migration downgrade has been
tested against a production-shaped backup.
