#!/bin/bash
set -euo pipefail

# =============================================================================
# PostgreSQL Restore Script
# =============================================================================
# Usage: pg_restore <backup_file> [--database NAME] [--create] [--drop]
# =============================================================================

BACKUP_FILE=""
DATABASE=""
CREATE_DB=false
DROP_DB=false
DRY_RUN=false

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --database|-d) DATABASE="$2"; shift 2 ;;
        --create) CREATE_DB=true; shift ;;
        --drop) DROP_DB=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --help|-h)
            echo "Usage: pg_restore <backup_file> [options]"
            echo ""
            echo "Options:"
            echo "  --database, -d NAME  Target database (default: from filename)"
            echo "  --create             Create database if not exists"
            echo "  --drop               Drop existing database first"
            echo "  --dry-run            Show what would be done"
            exit 0
            ;;
        -*) echo "Unknown option: $1"; exit 1 ;;
        *) BACKUP_FILE="$1"; shift ;;
    esac
done

if [[ -z "$BACKUP_FILE" ]]; then
    echo "Error: backup file required"
    echo "Usage: pg_restore <backup_file> [--database NAME]"
    exit 1
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "Error: file not found: $BACKUP_FILE"
    exit 1
fi

# Derive database name from filename if not specified
if [[ -z "$DATABASE" ]]; then
    DATABASE=$(basename "$BACKUP_FILE" | sed -E 's/_[0-9]{4}-[0-9]{2}-[0-9]{2}.*$//')
fi

# Determine decompression
case $BACKUP_FILE in
    *.zst) DECOMPRESS="zstd -d -c" ;;
    *.gz) DECOMPRESS="gzip -d -c" ;;
    *) DECOMPRESS="cat" ;;
esac

echo "Restore Plan:"
echo "  File:     $BACKUP_FILE"
echo "  Database: $DATABASE"
echo "  Drop:     $DROP_DB"
echo "  Create:   $CREATE_DB"
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY RUN] Would execute: $DECOMPRESS '$BACKUP_FILE' | psql -d '$DATABASE'"
    exit 0
fi

read -p "Continue? (y/N): " confirm
[[ ! "$confirm" =~ ^[Yy]$ ]] && exit 0

# Drop if requested
if [[ "$DROP_DB" == true ]]; then
    echo "Dropping database..."
    psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DATABASE';" 2>/dev/null || true
    dropdb --if-exists "$DATABASE"
fi

# Create if requested
if [[ "$CREATE_DB" == true ]]; then
    echo "Creating database..."
    createdb "$DATABASE" 2>/dev/null || true
fi

# Check database exists
if ! psql -lqt | cut -d \| -f 1 | grep -qw "$DATABASE"; then
    echo "Error: database '$DATABASE' does not exist. Use --create"
    exit 1
fi

# Restore
echo "Restoring..."
$DECOMPRESS "$BACKUP_FILE" | psql -d "$DATABASE" -v ON_ERROR_STOP=1 --quiet

echo ""
echo "Restore complete!"
echo "Run: psql -d $DATABASE -c 'ANALYZE;'"
