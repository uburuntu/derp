#!/bin/bash
set -euo pipefail

# =============================================================================
# PostgreSQL Database & User Creation Script
# =============================================================================
# Creates a database with a secure auto-generated password.
#
# Usage: sudo -u postgres pg_create_db <database_name> [username] [host] [port]
# =============================================================================

# Default host/port (replaced by install.sh)
DEFAULT_HOST="{{PG_PUBLIC_HOST}}"
DEFAULT_PORT="{{PG_PORT}}"

# Fallback if not replaced
[[ "$DEFAULT_HOST" == "{{PG_PUBLIC_HOST}}" ]] && DEFAULT_HOST="localhost"
[[ "$DEFAULT_PORT" == "{{PG_PORT}}" ]] && DEFAULT_PORT="5432"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Parse arguments
if [[ $# -lt 1 ]] || [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; then
    echo "Usage: pg_create_db <database_name> [username] [host] [port]"
    echo ""
    echo "Examples:"
    echo "  pg_create_db myapp"
    echo "  pg_create_db myapp myapp_user"
    echo "  pg_create_db myapp myapp_user db.example.com 5432"
    exit 0
fi

DB_NAME="$1"
DB_USER="${2:-${DB_NAME}_user}"
DB_HOST="${3:-$DEFAULT_HOST}"
DB_PORT="${4:-$DEFAULT_PORT}"

# Validate names
if [[ ! "$DB_NAME" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
    echo -e "${RED}Invalid database name: $DB_NAME${NC}"
    exit 1
fi

# Generate secure password
generate_password() {
    if command -v openssl &> /dev/null; then
        openssl rand -base64 24 | tr -d '\n' | tr '+/' '-_'
    else
        tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32
    fi
}

DB_PASSWORD=$(generate_password)

# Check if database exists
if psql -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    echo -e "${RED}Database '$DB_NAME' already exists!${NC}"
    exit 1
fi

# Check if user exists
USER_EXISTS=false
if psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    echo -e "${YELLOW}User '$DB_USER' exists, updating password${NC}"
    USER_EXISTS=true
fi

# Create user
if [[ "$USER_EXISTS" == false ]]; then
    psql -v ON_ERROR_STOP=1 --quiet << EOF
CREATE ROLE "$DB_USER" WITH 
    LOGIN 
    PASSWORD '$DB_PASSWORD'
    NOSUPERUSER NOCREATEDB NOCREATEROLE
    CONNECTION LIMIT 50;
EOF
else
    psql -v ON_ERROR_STOP=1 --quiet << EOF
ALTER ROLE "$DB_USER" WITH PASSWORD '$DB_PASSWORD';
EOF
fi

# Create database
psql -v ON_ERROR_STOP=1 --quiet << EOF
CREATE DATABASE "$DB_NAME" 
    WITH OWNER = '$DB_USER'
    ENCODING = 'UTF8'
    LC_COLLATE = 'en_US.UTF-8'
    LC_CTYPE = 'en_US.UTF-8'
    TEMPLATE = template0;
EOF

# Configure permissions
psql -d "$DB_NAME" -v ON_ERROR_STOP=1 --quiet << EOF
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE "$DB_NAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE "$DB_NAME" TO "$DB_USER";
GRANT USAGE, CREATE ON SCHEMA public TO "$DB_USER";
ALTER DEFAULT PRIVILEGES FOR ROLE "$DB_USER" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "$DB_USER";
ALTER DEFAULT PRIVILEGES FOR ROLE "$DB_USER" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO "$DB_USER";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
EOF

# URL-encode password
url_encode() {
    local string="$1"
    local encoded=""
    for (( i=0; i<${#string}; i++ )); do
        c="${string:$i:1}"
        case "$c" in
            [a-zA-Z0-9.~_-]) encoded+="$c" ;;
            *) printf -v hex '%%%02X' "'$c"; encoded+="$hex" ;;
        esac
    done
    echo "$encoded"
}

ENCODED_PASSWORD=$(url_encode "$DB_PASSWORD")
DATABASE_URL="postgresql://${DB_USER}:${ENCODED_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=require"
DATABASE_URL_ASYNC="postgresql+asyncpg://${DB_USER}:${ENCODED_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=require"

# Output
echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${GREEN}  Database Created Successfully${NC}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BOLD}Connection:${NC}"
echo -e "  Database:  ${CYAN}${DB_NAME}${NC}"
echo -e "  Username:  ${CYAN}${DB_USER}${NC}"
echo -e "  Host:      ${CYAN}${DB_HOST}${NC}"
echo -e "  Port:      ${CYAN}${DB_PORT}${NC}"
echo ""
echo -e "${BOLD}${YELLOW}Password (save this!):${NC}"
echo -e "  ${CYAN}${DB_PASSWORD}${NC}"
echo ""
echo -e "${BOLD}DATABASE_URL:${NC}"
echo -e "  ${CYAN}${DATABASE_URL}${NC}"
echo ""
echo -e "${BOLD}DATABASE_URL (async):${NC}"
echo -e "  ${CYAN}${DATABASE_URL_ASYNC}${NC}"
echo ""
echo -e "${YELLOW}⚠  Save these credentials now - password cannot be recovered!${NC}"
echo ""
