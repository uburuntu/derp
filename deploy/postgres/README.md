# PostgreSQL Production Deployment

Production-ready PostgreSQL with auto-tuned memory settings, security hardening, and automated backups.

## Quick Start

```bash
# Transfer to server
scp -r deploy/postgres user@server:/tmp/postgres-setup

# SSH and install
ssh user@server
cd /tmp/postgres-setup
chmod +x *.sh
sudo ./install.sh
```

That's it! The script:
- Detects your RAM and auto-tunes memory settings
- Enables remote access with SSL
- Sets up automated daily/weekly/monthly backups

## Create a Database

```bash
sudo -u postgres pg_create_db myapp
```

Outputs:
- Generated secure password
- `DATABASE_URL` ready for your `.env`

## Memory Tuning

Automatically configured based on server RAM:

| RAM | shared_buffers | effective_cache | work_mem |
|-----|----------------|-----------------|----------|
| 4GB | 512MB | 3GB | 4MB |
| 8GB | 1GB | 6GB | 8MB |
| 16GB | 2GB | 12GB | 16MB |
| 32GB | 4GB | 24GB | 32MB |
| 64GB+ | 8GB | 48GB | 64MB |

## Options

```bash
# Skip confirmation prompt
sudo ./install.sh --yes

# Fix common issues (kernel tuning, permissions)
sudo ./install.sh --fix

# Specify PostgreSQL version
sudo ./install.sh --version 17

# Set public hostname for DATABASE_URL
sudo ./install.sh --host db.example.com
```

**Re-running is safe** - the script detects existing installations and reconfigures without data loss.

## Commands

| Command | Description |
|---------|-------------|
| `sudo -u postgres pg_create_db <name>` | Create database with password |
| `sudo -u postgres pg_backup` | Manual backup |
| `sudo -u postgres pg_restore <file>` | Restore from backup |

## Backups

| Schedule | Retention | Time |
|----------|-----------|------|
| Daily | 7 days | 2:00 AM |
| Weekly | 4 weeks | Sunday 3:00 AM |
| Monthly | 6 months | 1st of month 4:00 AM |

```bash
# List backups
ls -la /var/backups/postgresql/daily/

# Check backup timers
systemctl list-timers | grep pg-backup

# View backup log
sudo tail -f /var/backups/postgresql/backup.log
```

## Security

- **SSL required** for all remote connections
- **scram-sha-256** password encryption
- Query/lock/idle timeouts configured
- Slow query logging (>1s)

## DATABASE_URL

After `pg_create_db`, you get:

```bash
# Standard
DATABASE_URL="postgresql://user:pass@host:5432/db?sslmode=require"

# Async (SQLAlchemy + asyncpg)
DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db?sslmode=require"
```

## Troubleshooting

### PostgreSQL won't start

```bash
# Check logs
sudo journalctl -xeu postgresql@18-main --no-pager | tail -30
sudo cat /var/log/postgresql/postgresql-18-main.log | tail -30
```

### Connection refused

```bash
# Check it's running
systemctl status postgresql@18-main

# Check port
ss -tlnp | grep 5432
```

### Reset to defaults

```bash
# Restore original config
sudo cp /etc/postgresql/18/main/postgresql.conf.original /etc/postgresql/18/main/postgresql.conf
sudo systemctl restart postgresql@18-main
```

## Uninstall

```bash
# Stop and remove PostgreSQL
sudo systemctl stop postgresql
sudo apt-get purge -y postgresql-18 postgresql-contrib-18 postgresql-client-18 postgresql-common

# Remove data
sudo rm -rf /var/lib/postgresql /etc/postgresql /var/log/postgresql /var/backups/postgresql

# Remove scripts
sudo rm -f /usr/local/bin/pg_create_db /usr/local/bin/pg_backup /usr/local/bin/pg_restore
sudo rm -f /etc/systemd/system/pg-backup*
sudo systemctl daemon-reload
```
