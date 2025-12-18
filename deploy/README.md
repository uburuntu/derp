# Deployment Guide

This guide covers deploying the Derp bot to production.

## Architecture

```
GitHub (push to main)
    ↓
CI (lint, test)
    ↓
Build Docker image → GitHub Container Registry (ghcr.io)
    ↓
SSH to server → Pull & deploy
    ↓
Health check (30s)
    ↓
✓ Success: Remove old container
✗ Failure: Rollback to previous
```

## Initial Server Setup

### 1. Set up PostgreSQL

```bash
# Transfer and run postgres setup
scp -r deploy/postgres user@server:/tmp/postgres-setup
ssh user@server
cd /tmp/postgres-setup
chmod +x *.sh
sudo ./install.sh

# Create database
sudo -u postgres pg_create_db derp
# Save the DATABASE_URL!
```

### 2. Set up deployment infrastructure

```bash
# Transfer and run server setup
scp deploy/setup-server.sh user@server:/tmp/
ssh user@server
sudo /tmp/setup-server.sh

# Edit environment file
sudo nano /opt/derp/.env.prod

# Login to GitHub Container Registry
docker login ghcr.io -u YOUR_GITHUB_USERNAME
# Use a PAT with read:packages scope
```

### 3. Configure GitHub Secrets

Go to: Repository → Settings → Secrets and variables → Actions

Add these secrets:

| Secret | Value |
|--------|-------|
| `SSH_HOST` | Your server IP (e.g., `192.145.37.23`) |
| `SSH_USER` | SSH username (e.g., `rmbk`) |
| `SSH_PRIVATE_KEY` | Your SSH private key (full content) |
| `DEPLOY_GHCR_TOKEN` | GitHub PAT with `read:packages` scope |

**Create the PAT:**
1. Go to: https://github.com/settings/tokens?type=beta
2. Generate new token (Fine-grained)
3. Select your repository
4. Permissions: Read access to packages
5. Copy token → add as `DEPLOY_GHCR_TOKEN` secret

### 4. Create GitHub Environment

Go to: Repository → Settings → Environments → New environment

- Name: `production`
- (Optional) Add required reviewers for manual approval

### 5. Generate SSH Key (if needed)

```bash
# On your local machine
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy

# Add public key to server
ssh-copy-id -i ~/.ssh/github_deploy.pub user@server

# Copy private key content for GitHub secret
cat ~/.ssh/github_deploy
```

## Deployment Flow

### Automatic Deployment

1. Create a PR with your changes
2. CI runs (lint, test)
3. Merge PR to `main`
4. CD automatically:
   - Builds Docker image
   - Pushes to ghcr.io
   - SSHs to server
   - Deploys with health check
   - Rolls back if unhealthy

### Manual Deployment

Trigger manually from: Actions → CD → Run workflow

### Rollback

If deployment fails:
- Automatic rollback to previous container
- Check logs: `docker logs derp-bot`
- Manual rollback: `docker start derp-bot-previous`

## Server Commands

```bash
# View running container
docker ps

# View logs
docker logs derp-bot -f

# Restart bot
docker restart derp-bot

# Check resource usage
docker stats derp-bot

# Manual deploy (pull latest)
docker pull ghcr.io/uburuntu/derp:latest
docker stop derp-bot && docker rm derp-bot
docker run -d --name derp-bot --restart unless-stopped \
  --env-file /opt/derp/.env.prod \
  ghcr.io/uburuntu/derp:latest
```

## Troubleshooting

### Deployment fails with permission denied

```bash
# On server - check docker socket permissions
sudo chmod 666 /var/run/docker.sock
# Or ensure user is in docker group
sudo usermod -aG docker $USER
newgrp docker
```

### Container keeps restarting

```bash
# Check logs for errors
docker logs derp-bot --tail 100

# Common issues:
# - Invalid DATABASE_URL in .env.prod
# - Missing environment variables
# - Database not accessible
```

### Can't pull from ghcr.io

```bash
# Re-authenticate
docker logout ghcr.io
docker login ghcr.io -u YOUR_GITHUB_USERNAME
# Use PAT with read:packages scope
```

### Health check fails

The health check waits 30 seconds for the container to stabilize. If it keeps failing:

```bash
# Check container logs
docker logs derp-bot

# Test manually
docker run --rm --env-file /opt/derp/.env.prod ghcr.io/uburuntu/derp:latest
```

## Environment Variables

Required in `/opt/derp/.env.prod`:

```bash
# Telegram
TELEGRAM_BOT_TOKEN=

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db?sslmode=require

# LLM
DEFAULT_LLM_MODEL=gemini-2.0-flash
GOOGLE_API_KEY=

# Observability
LOGFIRE_TOKEN=
ENVIRONMENT=prod
```

