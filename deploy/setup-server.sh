#!/bin/bash
set -euo pipefail

# =============================================================================
# Server Setup Script for Derp Bot Deployment
# =============================================================================
# Run this once on your production server to prepare for CD deployments.
#
# Usage: sudo ./setup-server.sh
# =============================================================================

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

# Check root
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)"
    exit 1
fi

DEPLOY_USER="${1:-rmbk}"
DEPLOY_DIR="/opt/derp"
GITHUB_REGISTRY="ghcr.io"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Server Setup for Derp Bot"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# =============================================================================
# Install Docker
# =============================================================================
log_step "Installing Docker..."

if command -v docker &> /dev/null; then
    log_info "Docker already installed: $(docker --version)"
else
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    
    log_info "Docker installed"
fi

# Add user to docker group
usermod -aG docker "$DEPLOY_USER" 2>/dev/null || true
log_info "Added $DEPLOY_USER to docker group"

# =============================================================================
# Create deployment directory
# =============================================================================
log_step "Setting up deployment directory..."

mkdir -p "$DEPLOY_DIR"
chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR"

# Create .env.prod template if not exists
if [[ ! -f "$DEPLOY_DIR/.env.prod" ]]; then
    cat > "$DEPLOY_DIR/.env.prod" << 'EOF'
# Derp Bot Production Environment
# Fill in your values and keep this file secure!

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Database (from pg_create_db output)
DATABASE_URL=postgresql+asyncpg://derp_user:PASSWORD@localhost:5432/derp?sslmode=require

# LLM
DEFAULT_LLM_MODEL=gemini-2.0-flash
GOOGLE_API_KEY=your_google_api_key
GOOGLE_API_EXTRA_KEYS=
GOOGLE_API_PAID_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=

# Observability
LOGFIRE_TOKEN=your_logfire_token
ENVIRONMENT=prod
EOF
    chmod 600 "$DEPLOY_DIR/.env.prod"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR/.env.prod"
    log_warn "Created $DEPLOY_DIR/.env.prod - EDIT THIS FILE with your secrets!"
else
    log_info ".env.prod already exists"
fi

# =============================================================================
# Configure GitHub Container Registry access
# =============================================================================
log_step "Configuring GitHub Container Registry..."

echo ""
log_warn "To pull images from GitHub Container Registry, you need to authenticate."
echo ""
echo "Run this as $DEPLOY_USER:"
echo "  docker login ghcr.io -u YOUR_GITHUB_USERNAME"
echo ""
echo "Use a Personal Access Token (PAT) with 'read:packages' scope as password."
echo "Create one at: https://github.com/settings/tokens"
echo ""

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Server Setup Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo ""
echo "1. Edit the environment file:"
echo "   sudo nano $DEPLOY_DIR/.env.prod"
echo ""
echo "2. Login to GitHub Container Registry (as $DEPLOY_USER):"
echo "   docker login ghcr.io -u YOUR_GITHUB_USERNAME"
echo ""
echo "3. Add these secrets to your GitHub repository:"
echo "   Settings → Secrets and variables → Actions → New repository secret"
echo ""
echo "   SSH_HOST      = $(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP')"
echo "   SSH_USER      = $DEPLOY_USER"
echo "   SSH_PRIVATE_KEY = (your SSH private key for $DEPLOY_USER)"
echo ""
echo "4. Create a 'production' environment in GitHub:"
echo "   Settings → Environments → New environment → 'production'"
echo ""
echo "5. Push to main branch to trigger deployment!"
echo ""

