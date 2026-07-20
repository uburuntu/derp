# Production deployment and recovery

The CD workflow builds one Linux/AMD64 image, pushes it to GHCR, and deploys
the exact manifest digest produced by the build. Tags such as `latest` are
discovery aids only; they are never deployment identities.

## Release invariants

- The GitHub `production` environment is the approval boundary. Approve a
  release only after verifying a current database backup.
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

After the database connects and Telegram `getMe` succeeds, the runtime writes a
private heartbeat that is refreshed by the application event loop. The Docker
health check requires that heartbeat to remain fresh, and deployment requires a
healthy, restart-free container for 60 seconds. This proves startup,
authentication, and event-loop liveness; it does not claim that a model
provider is reachable.

## Backup gate

Use the database provider's snapshot facility when available. Before approving
the GitHub production environment, record the snapshot identifier and confirm
that the provider reports it complete and restorable. Periodically restore a
snapshot into an isolated database; completion alone is not a restore test.

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

1. CI passes and the image is built, pushed, and attested.
2. The host pulls `ghcr.io/<owner>/<repo>@sha256:<digest>` and verifies that
   exact reference in Docker's `RepoDigests`.
3. The candidate prepares and verifies the persistent private artifact mount.
4. The candidate runs `alembic current` while the active bot is untouched. A
   bad image, environment file, or database connection fails here.
5. The active `derp-bot` is renamed to `derp-bot-previous` and fully stopped.
6. The candidate runs `alembic upgrade head`, followed by
   `alembic current --check-heads`.
7. The exact candidate starts as `derp-bot` with the artifact bind mount. Its
   digest is stored in Docker labels, and its event-loop heartbeat must become
   healthy during the readiness window.
8. When an active container existed, `derp-bot-previous` remains stopped. The
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
  "$IMAGE" alembic current
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
  "$IMAGE" alembic current --check-heads
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
