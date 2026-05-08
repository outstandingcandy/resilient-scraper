#!/usr/bin/env bash
#
# Start script for resilient-scraper service.
#
# Usage:
#   ./scripts/start.sh local    # Start locally via docker-compose
#   ./scripts/start.sh deploy   # Build & push to ECR, trigger ASG refresh
#   ./scripts/start.sh stop     # Stop local services
#   ./scripts/start.sh status   # Show service status
#
# Environment variables:
#   DB_URL       — Required. PostgreSQL connection string.
#   S3_BUCKET    — Optional. S3 bucket for raw data upload.
#   S3_PREFIX    — Optional. S3 key prefix.
#   ECR_REPO     — ECR repository URI (for deploy mode, or read from cdk-outputs.json).
#   ASG_NAME     — ASG name (for deploy mode, or read from cdk-outputs.json).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker/docker-compose.yml"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ─── Local mode ───────────────────────────────────────────────

cmd_local() {
    log_info "Starting resilient-scraper locally..."

    # Check required env vars
    if [ -z "${DB_URL:-}" ]; then
        log_error "DB_URL is required. Example:"
        echo "  DB_URL='postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/scraper' ./scripts/start.sh local"
        exit 1
    fi

    # Check AWS credentials (needed for S3 upload)
    if [ -n "${S3_BUCKET:-}" ]; then
        if [ ! -d "$HOME/.aws" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
            log_warn "S3_BUCKET is set but no AWS credentials found (~/.aws/ or AWS_ACCESS_KEY_ID)"
        fi
    fi

    # Build images
    log_info "Building Docker images..."
    docker compose -f "$COMPOSE_FILE" build

    # Start services
    log_info "Starting services..."
    docker compose -f "$COMPOSE_FILE" up -d

    # Wait for API health
    log_info "Waiting for API to become healthy..."
    local retries=30
    while [ $retries -gt 0 ]; do
        if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
            log_success "API is healthy"
            break
        fi
        retries=$((retries - 1))
        sleep 1
    done

    if [ $retries -eq 0 ]; then
        log_error "API failed to start. Check logs:"
        echo "  docker compose -f $COMPOSE_FILE logs api"
        exit 1
    fi

    echo ""
    log_success "Services are running!"
    echo ""
    echo -e "  API:     ${BOLD}http://localhost:8000${NC}"
    echo -e "  Health:  http://localhost:8000/health"
    echo -e "  Stats:   http://localhost:8000/stats"
    echo ""
    echo "  Logs:    docker compose -f $COMPOSE_FILE logs -f"
    echo "  Stop:    ./scripts/start.sh stop"
}

# ─── Deploy mode ──────────────────────────────────────────────

cmd_deploy() {
    log_info "Deploying to EC2 ASG..."

    # Resolve ECR repo
    local ecr_repo="${ECR_REPO:-}"
    local asg_name="${ASG_NAME:-}"

    # Try reading from cdk-outputs.json
    local outputs_file="$PROJECT_DIR/../infra/scraper/cdk.out/outputs.json"
    if [ -z "$ecr_repo" ] && [ -f "$outputs_file" ]; then
        ecr_repo=$(jq -r 'to_entries[0].value.ECRRepoURI // empty' "$outputs_file" 2>/dev/null || true)
    fi
    if [ -z "$asg_name" ] && [ -f "$outputs_file" ]; then
        asg_name=$(jq -r 'to_entries[0].value.ASGName // empty' "$outputs_file" 2>/dev/null || true)
    fi

    if [ -z "$ecr_repo" ]; then
        log_error "ECR_REPO is required. Set it via env var or deploy CDK stack first."
        exit 1
    fi
    if [ -z "$asg_name" ]; then
        log_error "ASG_NAME is required. Set it via env var or deploy CDK stack first."
        exit 1
    fi

    log_info "ECR repo: $ecr_repo"
    log_info "ASG name: $asg_name"

    # Build worker image
    log_info "Building worker Docker image..."
    docker build -t resilient-scraper-worker:latest \
        -f "$PROJECT_DIR/docker/Dockerfile.worker" "$PROJECT_DIR"

    # Login to ECR
    local ecr_host="${ecr_repo%%/*}"
    local region
    region=$(echo "$ecr_host" | grep -oP '(?<=\.)[a-z]+-[a-z]+-[0-9]+(?=\.)' || echo "us-east-1")

    log_info "Logging in to ECR ($ecr_host)..."
    aws ecr get-login-password --region "$region" | \
        docker login --username AWS --password-stdin "$ecr_host"

    # Tag and push
    local image_tag="worker-$(date +%Y%m%d-%H%M%S)"
    log_info "Pushing image with tag: $image_tag"
    docker tag resilient-scraper-worker:latest "${ecr_repo}:${image_tag}"
    docker tag resilient-scraper-worker:latest "${ecr_repo}:latest"
    docker push "${ecr_repo}:${image_tag}"
    docker push "${ecr_repo}:latest"

    # Trigger ASG instance refresh
    log_info "Starting ASG instance refresh..."
    aws autoscaling start-instance-refresh \
        --auto-scaling-group-name "$asg_name" \
        --preferences '{"MinHealthyPercentage": 50, "InstanceWarmup": 300}' \
        >/dev/null 2>&1 || {
            log_warn "Instance refresh already in progress or failed to start"
        }

    log_success "Deploy complete! ASG instance refresh started."
    echo ""
    echo "  Monitor: aws autoscaling describe-instance-refreshes --auto-scaling-group-name $asg_name"
}

# ─── Stop ─────────────────────────────────────────────────────

cmd_stop() {
    log_info "Stopping local services..."
    docker compose -f "$COMPOSE_FILE" down
    log_success "Services stopped."
}

# ─── Status ───────────────────────────────────────────────────

cmd_status() {
    echo -e "${BOLD}Local services:${NC}"
    docker compose -f "$COMPOSE_FILE" ps 2>/dev/null || echo "  (not running)"
    echo ""

    # Try to hit the API
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        echo -e "${BOLD}API health:${NC}"
        curl -s http://localhost:8000/health | python3 -m json.tool 2>/dev/null || true
        echo ""
        echo -e "${BOLD}Queue stats:${NC}"
        curl -s http://localhost:8000/stats | python3 -m json.tool 2>/dev/null || true
    fi
}

# ─── Main ─────────────────────────────────────────────────────

case "${1:-}" in
    local)  cmd_local ;;
    deploy) cmd_deploy ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    *)
        echo "Usage: $0 {local|deploy|stop|status}"
        echo ""
        echo "  local   — Start API + Worker via docker-compose"
        echo "  deploy  — Build & push to ECR, trigger ASG instance refresh"
        echo "  stop    — Stop local docker-compose services"
        echo "  status  — Show service status"
        exit 1
        ;;
esac
