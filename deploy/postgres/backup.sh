#!/bin/bash
set -euo pipefail

# =============================================================================
# PostgreSQL Backup Script
# =============================================================================
# Usage: pg_backup [--type daily|weekly|monthly] [--database DB]
# =============================================================================

BACKUP_ROOT="${PG_BACKUP_DIR:-/var/backups/postgresql}"
COMPRESSION="${PG_BACKUP_COMPRESSION:-zstd}"
BACKUP_TYPE="daily"
DATABASES="all"

# Retention
DAILY_KEEP=7
WEEKLY_KEEP=4
MONTHLY_KEEP=6

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --type) BACKUP_TYPE="$2"; shift 2 ;;
        --database) DATABASES="$2"; shift 2 ;;
        *) shift ;;
    esac
done

BACKUP_DIR="${BACKUP_ROOT}/${BACKUP_TYPE}"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOG_FILE="${BACKUP_ROOT}/backup.log"

mkdir -p "$BACKUP_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "Starting $BACKUP_TYPE backup"

# Get databases
get_databases() {
    if [[ "$DATABASES" == "all" ]]; then
        psql -At -c "SELECT datname FROM pg_database WHERE datistemplate = false AND datname != 'postgres'"
    else
        echo "$DATABASES"
    fi
}

# Backup each database
for db in $(get_databases); do
    log "Backing up: $db"
    
    case $COMPRESSION in
        zstd)
            backup_file="${BACKUP_DIR}/${db}_${TIMESTAMP}.sql.zst"
            pg_dump -Fp "$db" 2>/dev/null | zstd -T0 -q > "$backup_file"
            ;;
        gzip)
            backup_file="${BACKUP_DIR}/${db}_${TIMESTAMP}.sql.gz"
            pg_dump -Fp "$db" 2>/dev/null | gzip > "$backup_file"
            ;;
        *)
            backup_file="${BACKUP_DIR}/${db}_${TIMESTAMP}.sql"
            pg_dump -Fp "$db" > "$backup_file" 2>/dev/null
            ;;
    esac
    
    if [[ -f "$backup_file" ]] && [[ -s "$backup_file" ]]; then
        size=$(du -h "$backup_file" | cut -f1)
        log "  OK: $backup_file ($size)"
    else
        log "  FAILED: $db"
    fi
done

# Backup globals
log "Backing up roles..."
pg_dumpall --globals-only > "${BACKUP_DIR}/globals_${TIMESTAMP}.sql" 2>/dev/null

# Rotate old backups
rotate() {
    local dir=$1 keep=$2
    local count=$(find "$dir" -name "*.sql*" -type f 2>/dev/null | wc -l)
    if [[ $count -gt $keep ]]; then
        find "$dir" -name "*.sql*" -type f -printf '%T+ %p\n' | \
            sort | head -n $((count - keep)) | cut -d' ' -f2- | \
            xargs rm -f
        log "Rotated backups in $dir (kept $keep)"
    fi
}

case $BACKUP_TYPE in
    daily) rotate "$BACKUP_DIR" $DAILY_KEEP ;;
    weekly) rotate "$BACKUP_DIR" $WEEKLY_KEEP ;;
    monthly) rotate "$BACKUP_DIR" $MONTHLY_KEEP ;;
esac

# Clean old WAL archives
find "${BACKUP_ROOT}/wal_archive" -type f -mtime +7 -delete 2>/dev/null || true

log "Backup complete"
