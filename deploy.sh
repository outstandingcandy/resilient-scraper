#!/usr/bin/env bash
#
# Deploy resilient-scraper workers to AWS.
#
# Usage:
#   ./deploy.sh deploy     # First deploy or full infrastructure update
#   ./deploy.sh update     # Code update → rebuild image + rolling instance refresh
#   ./deploy.sh scale N    # Manually set desired instance count
#   ./deploy.sh status     # Show ASG status
#   ./deploy.sh destroy    # Tear down all infrastructure
#
# Configuration is read from .env (shared with scripts/run.sh).
# Required variables: VPC_ID, DB_SG_ID, DB_URL
# SSM parameters are synced automatically from .env on deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$SCRIPT_DIR/infra"
STACK_NAME="ResilientScraperStack"
ENV_FILE="$SCRIPT_DIR/.env"

# Load .env
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ─── Helpers ────────────────────────────────────────────────

_check_prereqs() {
    local missing=()
    command -v aws &>/dev/null || missing+=(aws-cli)
    command -v cdk &>/dev/null || missing+=(aws-cdk)
    command -v docker &>/dev/null || missing+=(docker)
    command -v python3 &>/dev/null || missing+=(python3)

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "Missing prerequisites: ${missing[*]}"
        echo "Install with: npm install -g aws-cdk"
        exit 1
    fi
}

_get_asg_name() {
    aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query 'Stacks[0].Outputs[?OutputKey==`ASGName`].OutputValue' \
        --output text \
        --region "${CDK_DEFAULT_REGION:-us-east-1}" 2>/dev/null
}

_ensure_ssm_param() {
    # Usage: _ensure_ssm_param NAME VALUE TYPE
    # Creates SSM parameter if it doesn't exist, updates if value changed.
    local name="$1" value="$2" type="${3:-SecureString}"
    local region="${CDK_DEFAULT_REGION:-us-east-1}"

    if [ -z "$value" ]; then
        return
    fi

    local existing
    existing=$(aws ssm get-parameter --name "$name" --with-decryption \
        --query 'Parameter.Value' --output text --region "$region" 2>/dev/null || true)

    if [ -z "$existing" ]; then
        aws ssm put-parameter --name "$name" --type "$type" \
            --value "$value" --region "$region" >/dev/null
        log_ok "SSM created: $name"
    elif [ "$existing" != "$value" ]; then
        aws ssm put-parameter --name "$name" --type "$type" \
            --value "$value" --overwrite --region "$region" >/dev/null
        log_ok "SSM updated: $name"
    fi
}

_ensure_ssm_params() {
    log_info "Syncing SSM parameters from .env..."
    _ensure_ssm_param /resilient-scraper/db-url "${DB_URL:-}" SecureString
    _ensure_ssm_param /resilient-scraper/feishu-app-id "${FEISHU_APP_ID:-}" String
    _ensure_ssm_param /resilient-scraper/feishu-app-secret "${FEISHU_APP_SECRET:-}" SecureString
    _ensure_ssm_param /resilient-scraper/s3-bucket "${S3_BUCKET:-}" String
    _ensure_ssm_param /resilient-scraper/s3-prefix "${S3_PREFIX:-}" String
}

_cdk_context_args() {
    local args=""
    [ -n "${VPC_ID:-}" ] && args="$args -c vpc_id=$VPC_ID"
    [ -n "${DB_SG_ID:-}" ] && args="$args -c db_sg_id=$DB_SG_ID"
    [ -n "${RSCRAPER_INSTANCE_TYPE:-}" ] && args="$args -c instance_type=$RSCRAPER_INSTANCE_TYPE"
    [ -n "${AUTOSCALE_MIN_INSTANCES:-}" ] && args="$args -c min_capacity=$AUTOSCALE_MIN_INSTANCES"
    [ -n "${AUTOSCALE_MAX_INSTANCES:-}" ] && args="$args -c max_capacity=$AUTOSCALE_MAX_INSTANCES"
    echo "$args"
}

# ─── Commands ───────────────────────────────────────────────

cmd_deploy() {
    _check_prereqs

    if [ -z "${VPC_ID:-}" ] || [ -z "${DB_SG_ID:-}" ]; then
        log_error "VPC_ID and DB_SG_ID are required"
        echo "  export VPC_ID=vpc-xxx"
        echo "  export DB_SG_ID=sg-xxx"
        exit 1
    fi

    log_info "Deploying resilient-scraper infrastructure..."

    # Sync secrets to SSM Parameter Store
    _ensure_ssm_params

    # Ensure CDK is bootstrapped
    cd "$INFRA_DIR"
    if ! aws cloudformation describe-stacks --stack-name CDKToolkit --region "${CDK_DEFAULT_REGION:-us-east-1}" &>/dev/null; then
        log_info "Bootstrapping CDK..."
        : "${CDK_DEFAULT_ACCOUNT:?CDK_DEFAULT_ACCOUNT must be set}"
        cdk bootstrap "aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION:-us-east-1}"
    fi

    # Install CDK Python deps if needed
    if [ ! -d ".venv-cdk" ]; then
        python3 -m venv .venv-cdk
        .venv-cdk/bin/pip install -q -r requirements.txt
    fi

    # Activate venv so `cdk --app "python3 app.py"` uses the right Python
    export VIRTUAL_ENV="$INFRA_DIR/.venv-cdk"
    export PATH="$VIRTUAL_ENV/bin:$PATH"

    # Deploy
    local ctx_args
    ctx_args=$(_cdk_context_args)
    # shellcheck disable=SC2086
    cdk deploy --app "python3 app.py" \
        --require-approval never $ctx_args

    # Write ASG name back to .env for API auto-scale
    local asg_name
    asg_name=$(_get_asg_name)
    if [ -n "$asg_name" ] && [ -f "$ENV_FILE" ]; then
        if grep -q '^AUTOSCALE_ASG_NAME=' "$ENV_FILE"; then
            sed -i "s|^AUTOSCALE_ASG_NAME=.*|AUTOSCALE_ASG_NAME=$asg_name|" "$ENV_FILE"
        else
            echo "AUTOSCALE_ASG_NAME=$asg_name" >> "$ENV_FILE"
        fi
        log_ok "AUTOSCALE_ASG_NAME=$asg_name written to .env"
    fi

    log_ok "Deployment complete!"
    echo ""
    cmd_status
}

cmd_update() {
    _check_prereqs
    log_info "Updating resilient-scraper (rebuild image + instance refresh)..."

    cd "$INFRA_DIR"
    export VIRTUAL_ENV="$INFRA_DIR/.venv-cdk"
    export PATH="$VIRTUAL_ENV/bin:$PATH"

    local ctx_args
    ctx_args=$(_cdk_context_args)

    # CDK deploy will rebuild the Docker image and update the launch template
    # shellcheck disable=SC2086
    cdk deploy --app "python3 app.py" \
        --require-approval never $ctx_args

    # Trigger rolling instance refresh
    local asg_name
    asg_name=$(_get_asg_name)
    if [ -n "$asg_name" ]; then
        log_info "Starting instance refresh for $asg_name..."
        aws autoscaling start-instance-refresh \
            --auto-scaling-group-name "$asg_name" \
            --preferences '{"MinHealthyPercentage":0,"InstanceWarmup":300}' \
            --region "${CDK_DEFAULT_REGION:-us-east-1}" >/dev/null
        log_ok "Instance refresh started (rolling replacement)"
    fi
}

cmd_scale() {
    local count="${1:?Usage: deploy.sh scale N}"
    local asg_name
    asg_name=$(_get_asg_name)
    if [ -z "$asg_name" ]; then
        log_error "ASG not found. Run 'deploy.sh deploy' first."
        exit 1
    fi

    log_info "Scaling $asg_name to $count instances..."
    aws autoscaling update-auto-scaling-group \
        --auto-scaling-group-name "$asg_name" \
        --desired-capacity "$count" \
        --region "${CDK_DEFAULT_REGION:-us-east-1}"
    log_ok "Desired capacity set to $count"
}

cmd_status() {
    local region="${CDK_DEFAULT_REGION:-us-east-1}"
    local asg_name
    asg_name=$(_get_asg_name)

    if [ -z "$asg_name" ]; then
        log_error "Stack not deployed yet. Run 'deploy.sh deploy' first."
        exit 1
    fi

    echo -e "${BOLD}ASG: $asg_name${NC}"
    aws autoscaling describe-auto-scaling-groups \
        --auto-scaling-group-names "$asg_name" \
        --region "$region" \
        --query 'AutoScalingGroups[0].{Min:MinSize,Max:MaxSize,Desired:DesiredCapacity,InService:length(Instances[?LifecycleState==`InService`])}' \
        --output table 2>/dev/null

    # Instance details
    local instances
    instances=$(aws autoscaling describe-auto-scaling-groups \
        --auto-scaling-group-names "$asg_name" \
        --region "$region" \
        --query 'AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]' \
        --output text 2>/dev/null)
    if [ -n "$instances" ]; then
        echo -e "\n${BOLD}Instances:${NC}"
        echo "$instances" | while read -r id state health; do
            echo "  $id  $state  $health"
        done
    else
        echo -e "\n  No instances running"
    fi
}

cmd_destroy() {
    _check_prereqs
    log_info "Destroying resilient-scraper infrastructure..."
    cd "$INFRA_DIR"
    export VIRTUAL_ENV="$INFRA_DIR/.venv-cdk"
    export PATH="$VIRTUAL_ENV/bin:$PATH"

    local ctx_args
    ctx_args=$(_cdk_context_args)
    # shellcheck disable=SC2086
    cdk destroy --app "python3 app.py" \
        --force $ctx_args
    log_ok "Infrastructure destroyed"
}

# ─── Main ───────────────────────────────────────────────────

case "${1:-}" in
    deploy)  cmd_deploy ;;
    update)  cmd_update ;;
    scale)   cmd_scale "${2:-}" ;;
    status)  cmd_status ;;
    destroy) cmd_destroy ;;
    *)
        echo "Usage: $0 {deploy|update|scale N|status|destroy}"
        echo ""
        echo "Configuration is read from .env. Key variables:"
        echo "  VPC_ID                    VPC ID (required)"
        echo "  DB_SG_ID                  Aurora security group ID (required)"
        echo "  AUTOSCALE_MIN_INSTANCES   ASG min instances (default: 0)"
        echo "  AUTOSCALE_MAX_INSTANCES   ASG max instances (default: 5)"
        exit 1
        ;;
esac
