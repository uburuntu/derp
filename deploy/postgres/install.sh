#!/bin/bash
set -euo pipefail

# =============================================================================
# PostgreSQL Production Installation Script
# =============================================================================
# Supports: Ubuntu 22.04/24.04, Debian 11/12
# Re-runnable: Yes - safely updates config on existing installations
#
# Usage:
#   sudo ./install.sh                    # Install or reconfigure
#   sudo ./install.sh --public           # Enable public access
#   sudo ./install.sh --yes              # Skip confirmation
#   sudo ./install.sh --fix              # Fix common issues only
# =============================================================================

# Default Configuration
PG_VERSION="${PG_VERSION:-18}"
PG_PORT="${PG_PORT:-5432}"
PG_ENABLE_REMOTE="${PG_ENABLE_REMOTE:-true}"
PG_PUBLIC_HOST="${PG_PUBLIC_HOST:-}"
PG_DATA_DIR="/var/lib/postgresql"
PG_BACKUP_DIR="/var/backups/postgresql"
PG_LOG_DIR="/var/log/postgresql"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

# Parse arguments
SKIP_CONFIRM=false
FIX_ONLY=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
    case $1 in
        --public) PG_ENABLE_REMOTE="true"; shift ;;
        --yes|-y) SKIP_CONFIRM=true; shift ;;
        --fix) FIX_ONLY=true; SKIP_CONFIRM=true; shift ;;
        --version) PG_VERSION="$2"; shift 2 ;;
        --host) PG_PUBLIC_HOST="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: sudo ./install.sh [options]"
            echo ""
            echo "Options:"
            echo "  --public     Enable remote access"
            echo "  --yes, -y    Skip confirmation prompts"
            echo "  --fix        Fix common issues only (no reinstall)"
            echo "  --version V  PostgreSQL version (default: 18)"
            echo "  --host HOST  Public hostname/IP"
            exit 0
            ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

# Pre-flight checks
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

# Detect OS
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS=$ID
    OS_VERSION=$VERSION_ID
else
    log_error "Cannot detect OS. This script supports Ubuntu/Debian only."
    exit 1
fi

# Check if PostgreSQL is already installed
PG_INSTALLED=false
if command -v psql &> /dev/null && [[ -d "/etc/postgresql/${PG_VERSION}" ]]; then
    PG_INSTALLED=true
fi

# Auto-detect public IP
if [[ -z "$PG_PUBLIC_HOST" ]]; then
    PG_PUBLIC_HOST=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "localhost")
fi

# Get system resources
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_MB=$((TOTAL_RAM_KB / 1024))
TOTAL_RAM_GB=$((TOTAL_RAM_MB / 1024))
CPU_COUNT=$(nproc)

# Calculate memory settings based on RAM (conservative values)
if [[ $TOTAL_RAM_GB -ge 64 ]]; then
    SHARED_BUFFERS="8GB"
    EFFECTIVE_CACHE="48GB"
    MAINTENANCE_MEM="2GB"
    WORK_MEM="64MB"
elif [[ $TOTAL_RAM_GB -ge 32 ]]; then
    SHARED_BUFFERS="4GB"
    EFFECTIVE_CACHE="24GB"
    MAINTENANCE_MEM="1GB"
    WORK_MEM="32MB"
elif [[ $TOTAL_RAM_GB -ge 16 ]]; then
    SHARED_BUFFERS="2GB"
    EFFECTIVE_CACHE="12GB"
    MAINTENANCE_MEM="512MB"
    WORK_MEM="16MB"
elif [[ $TOTAL_RAM_GB -ge 8 ]]; then
    SHARED_BUFFERS="1GB"
    EFFECTIVE_CACHE="6GB"
    MAINTENANCE_MEM="256MB"
    WORK_MEM="8MB"
elif [[ $TOTAL_RAM_GB -ge 4 ]]; then
    SHARED_BUFFERS="512MB"
    EFFECTIVE_CACHE="3GB"
    MAINTENANCE_MEM="128MB"
    WORK_MEM="4MB"
else
    SHARED_BUFFERS="256MB"
    EFFECTIVE_CACHE="768MB"
    MAINTENANCE_MEM="64MB"
    WORK_MEM="4MB"
fi

# =============================================================================
# Fix common issues (always run)
# =============================================================================
fix_common_issues() {
    log_step "Fixing common issues..."
    
    # Fix kernel shared memory limits (most common issue)
    cat > /etc/sysctl.d/99-postgresql.conf << EOF
# PostgreSQL kernel tuning
vm.swappiness = 1
vm.dirty_ratio = 10
vm.dirty_background_ratio = 3

# Shared memory limits for PostgreSQL
kernel.shmmax = 2147483648
kernel.shmall = 524288

# Do NOT set vm.overcommit_memory=2 - it breaks shared memory allocation
EOF
    sysctl -p /etc/sysctl.d/99-postgresql.conf 2>/dev/null || true
    log_info "Applied kernel tuning"
    
    # Ensure directories exist with correct permissions
    mkdir -p "${PG_BACKUP_DIR}"/{daily,weekly,monthly,wal_archive} 2>/dev/null || true
    mkdir -p "${PG_LOG_DIR}" 2>/dev/null || true
    chown -R postgres:postgres "${PG_BACKUP_DIR}" "${PG_LOG_DIR}" 2>/dev/null || true
    chmod 700 "${PG_BACKUP_DIR}" 2>/dev/null || true
    
    # Ensure run directory exists
    mkdir -p /run/postgresql 2>/dev/null || true
    chown postgres:postgres /run/postgresql 2>/dev/null || true
    
    log_info "Fixed directory permissions"
}

# Run fixes first
fix_common_issues

# If --fix only, try to restart and exit
if [[ "$FIX_ONLY" == true ]]; then
    if [[ "$PG_INSTALLED" == true ]]; then
        log_step "Attempting to restart PostgreSQL..."
        systemctl restart "postgresql@${PG_VERSION}-main" 2>/dev/null || true
        sleep 2
        if systemctl is-active --quiet "postgresql@${PG_VERSION}-main"; then
            log_info "PostgreSQL is running!"
            exit 0
        else
            log_warn "PostgreSQL still not running. Try full reinstall: sudo ./install.sh"
            exit 1
        fi
    else
        log_warn "PostgreSQL not installed. Run: sudo ./install.sh"
        exit 1
    fi
fi

# Display configuration
echo ""
echo -e "${BOLD}PostgreSQL ${PG_VERSION} Installation${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  OS:              $OS $OS_VERSION"
echo "  RAM:             ${TOTAL_RAM_GB}GB (${CPU_COUNT} CPUs)"
echo "  shared_buffers:  $SHARED_BUFFERS"
echo "  effective_cache: $EFFECTIVE_CACHE"
echo "  Remote access:   $PG_ENABLE_REMOTE"
echo "  Public host:     $PG_PUBLIC_HOST"
if [[ "$PG_INSTALLED" == true ]]; then
    echo -e "  Status:          ${YELLOW}Already installed - will reconfigure${NC}"
fi
echo ""

if [[ "$SKIP_CONFIRM" != "true" ]]; then
    read -p "Continue? (Y/n): " confirm
    [[ "$confirm" =~ ^[Nn]$ ]] && exit 0
fi

# =============================================================================
# Install PostgreSQL (skip if already installed)
# =============================================================================
if [[ "$PG_INSTALLED" != true ]]; then
    log_step "Installing PostgreSQL ${PG_VERSION}..."

    apt-get update -qq
    apt-get install -y -qq curl ca-certificates gnupg lsb-release

    install -d /usr/share/postgresql-common/pgdg
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
        gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg 2>/dev/null

    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list

    apt-get update -qq
    apt-get install -y -qq \
        "postgresql-${PG_VERSION}" \
        "postgresql-contrib-${PG_VERSION}" \
        "postgresql-client-${PG_VERSION}"

    log_info "PostgreSQL ${PG_VERSION} installed"
else
    log_info "PostgreSQL ${PG_VERSION} already installed, reconfiguring..."
fi

# =============================================================================
# Stop PostgreSQL for reconfiguration
# =============================================================================
systemctl stop "postgresql@${PG_VERSION}-main" 2>/dev/null || true

# =============================================================================
# Configure PostgreSQL
# =============================================================================
log_step "Configuring PostgreSQL..."

PG_CONF="/etc/postgresql/${PG_VERSION}/main/postgresql.conf"
PG_HBA="/etc/postgresql/${PG_VERSION}/main/pg_hba.conf"

# Backup original configs (only if not already backed up)
[[ -f "${PG_CONF}" && ! -f "${PG_CONF}.original" ]] && cp "${PG_CONF}" "${PG_CONF}.original"
[[ -f "${PG_HBA}" && ! -f "${PG_HBA}.original" ]] && cp "${PG_HBA}" "${PG_HBA}.original"

# Write complete postgresql.conf
cat > "${PG_CONF}" << EOF
# PostgreSQL ${PG_VERSION} Configuration
# Generated by install.sh on $(date -Iseconds)
# Server: ${TOTAL_RAM_GB}GB RAM, ${CPU_COUNT} CPUs

# =============================================================================
# File Locations
# =============================================================================
data_directory = '${PG_DATA_DIR}/${PG_VERSION}/main'
hba_file = '/etc/postgresql/${PG_VERSION}/main/pg_hba.conf'
ident_file = '/etc/postgresql/${PG_VERSION}/main/pg_ident.conf'
external_pid_file = '/var/run/postgresql/${PG_VERSION}-main.pid'
unix_socket_directories = '/var/run/postgresql'

# =============================================================================
# Connection Settings
# =============================================================================
listen_addresses = '*'
port = ${PG_PORT}
max_connections = 100
superuser_reserved_connections = 3

# =============================================================================
# Memory (tuned for ${TOTAL_RAM_GB}GB RAM)
# =============================================================================
shared_buffers = ${SHARED_BUFFERS}
effective_cache_size = ${EFFECTIVE_CACHE}
maintenance_work_mem = ${MAINTENANCE_MEM}
work_mem = ${WORK_MEM}
huge_pages = try

# =============================================================================
# Write-Ahead Log
# =============================================================================
wal_level = replica
wal_buffers = 64MB
max_wal_size = 2GB
min_wal_size = 512MB
checkpoint_completion_target = 0.9
checkpoint_timeout = 15min

# =============================================================================
# Query Planner (SSD optimized)
# =============================================================================
random_page_cost = 1.1
effective_io_concurrency = 200
default_statistics_target = 100

# =============================================================================
# Parallel Query
# =============================================================================
max_worker_processes = ${CPU_COUNT}
max_parallel_workers_per_gather = 2
max_parallel_workers = ${CPU_COUNT}
max_parallel_maintenance_workers = 2

# =============================================================================
# Logging
# =============================================================================
logging_collector = on
log_directory = '${PG_LOG_DIR}'
log_filename = 'postgresql-%Y-%m-%d.log'
log_rotation_age = 1d
log_rotation_size = 100MB
log_min_duration_statement = 1000
log_checkpoints = on
log_connections = on
log_disconnections = on
log_lock_waits = on
log_line_prefix = '%t [%p]: user=%u,db=%d,app=%a,client=%h '

# =============================================================================
# Autovacuum
# =============================================================================
autovacuum = on
autovacuum_max_workers = 3
autovacuum_vacuum_scale_factor = 0.02
autovacuum_analyze_scale_factor = 0.01

# =============================================================================
# Security
# =============================================================================
ssl = on
ssl_cert_file = '/etc/ssl/certs/ssl-cert-snakeoil.pem'
ssl_key_file = '/etc/ssl/private/ssl-cert-snakeoil.key'
password_encryption = scram-sha-256

# =============================================================================
# Timeouts
# =============================================================================
statement_timeout = 60000
lock_timeout = 10000
idle_in_transaction_session_timeout = 600000

# =============================================================================
# Locale
# =============================================================================
datestyle = 'iso, mdy'
timezone = 'Europe/London'
lc_messages = 'en_US.UTF-8'
lc_monetary = 'en_US.UTF-8'
lc_numeric = 'en_US.UTF-8'
lc_time = 'en_US.UTF-8'
default_text_search_config = 'pg_catalog.english'
EOF

log_info "Created postgresql.conf"

# Write pg_hba.conf
cat > "${PG_HBA}" << EOF
# PostgreSQL Client Authentication Configuration
# Generated by install.sh

# Local connections
local   all             postgres                                peer
local   all             all                                     scram-sha-256

# IPv4/IPv6 localhost
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
EOF

# Add remote access if enabled
if [[ "$PG_ENABLE_REMOTE" == "true" ]]; then
    cat >> "${PG_HBA}" << EOF

# Remote connections (all IPs - use firewall to restrict)
hostssl all             all             0.0.0.0/0               scram-sha-256
hostssl all             all             ::/0                    scram-sha-256
EOF
    log_info "Enabled remote access via SSL"
fi

# Set ownership
chown postgres:postgres "${PG_CONF}" "${PG_HBA}"
chmod 640 "${PG_CONF}" "${PG_HBA}"

# =============================================================================
# Firewall
# =============================================================================
if [[ "$PG_ENABLE_REMOTE" == "true" ]]; then
    if command -v ufw &> /dev/null && ufw status | grep -q "Status: active"; then
        ufw allow ${PG_PORT}/tcp > /dev/null 2>&1 || true
        log_info "Opened firewall port ${PG_PORT}"
    fi
fi

# =============================================================================
# Start PostgreSQL
# =============================================================================
log_step "Starting PostgreSQL..."

systemctl start "postgresql@${PG_VERSION}-main"
systemctl enable "postgresql@${PG_VERSION}-main" 2>/dev/null || true

sleep 2

if systemctl is-active --quiet "postgresql@${PG_VERSION}-main"; then
    log_info "PostgreSQL ${PG_VERSION} is running"
else
    log_error "PostgreSQL failed to start!"
    echo ""
    echo "Try these fixes:"
    echo "  1. Check logs: sudo journalctl -xeu postgresql@${PG_VERSION}-main"
    echo "  2. Run fix: sudo ./install.sh --fix"
    echo "  3. Reduce memory: edit /etc/postgresql/${PG_VERSION}/main/postgresql.conf"
    echo "     Set: shared_buffers = 256MB"
    exit 1
fi

# =============================================================================
# Install helper scripts
# =============================================================================
log_step "Installing helper scripts..."

for script in create-db.sh backup.sh restore.sh; do
    if [[ -f "${SCRIPT_DIR}/${script}" ]]; then
        dest="/usr/local/bin/pg_${script%.sh}"
        [[ "$script" == "create-db.sh" ]] && dest="/usr/local/bin/pg_create_db"
        
        sed -e "s|{{PG_PUBLIC_HOST}}|${PG_PUBLIC_HOST}|g" \
            -e "s|{{PG_PORT}}|${PG_PORT}|g" \
            "${SCRIPT_DIR}/${script}" > "$dest"
        chmod +x "$dest"
    fi
done

log_info "Installed: pg_create_db, pg_backup, pg_restore"

# Install backup timers
for f in pg-backup.service pg-backup.timer pg-backup-weekly.service pg-backup-weekly.timer pg-backup-monthly.service pg-backup-monthly.timer; do
    [[ -f "${SCRIPT_DIR}/${f}" ]] && cp "${SCRIPT_DIR}/${f}" /etc/systemd/system/
done

systemctl daemon-reload
systemctl enable pg-backup.timer pg-backup-weekly.timer pg-backup-monthly.timer 2>/dev/null || true
systemctl start pg-backup.timer pg-backup-weekly.timer pg-backup-monthly.timer 2>/dev/null || true

log_info "Enabled automated backups"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${GREEN}  PostgreSQL ${PG_VERSION} Ready!${NC}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BOLD}Memory Configuration:${NC}"
echo "  shared_buffers:     ${SHARED_BUFFERS}"
echo "  effective_cache:    ${EFFECTIVE_CACHE}"
echo "  maintenance_work:   ${MAINTENANCE_MEM}"
echo "  work_mem:           ${WORK_MEM}"
echo ""
echo -e "${BOLD}Create a database:${NC}"
echo "  sudo -u postgres pg_create_db myapp"
echo ""
if [[ "$PG_ENABLE_REMOTE" == "true" ]]; then
    echo -e "${BOLD}Remote connection:${NC}"
    echo "  Host: ${PG_PUBLIC_HOST}"
    echo "  Port: ${PG_PORT}"
    echo "  SSL:  required"
    echo ""
fi
echo -e "${BOLD}Troubleshooting:${NC}"
echo "  Fix issues:  sudo ./install.sh --fix"
echo "  View logs:   sudo tail -f ${PG_LOG_DIR}/postgresql-$(date +%Y-%m-%d).log"
echo ""
