#!/usr/bin/env bash
#
# Submit scraping tasks to the resilient-scraper API.
#
# Usage:
#   ./scripts/submit.sh                              # Submit from config/xhs.conf
#   ./scripts/submit.sh <user_id> [user_id ...]      # Scrape specific users
#   ./scripts/submit.sh --file config/xhs.conf       # Submit from config file
#   ./scripts/submit.sh --type ebay_store --file config/ebay.conf
#   ./scripts/submit.sh --status <task_id>            # Check task status
#   ./scripts/submit.sh --list                        # List active tasks
#
# Config file format (config/xhs.conf):
#   skip_existing_days=30       # key=value lines set payload parameters
#   max_notes=0
#   411325471                   # other lines are user IDs
#
# Examples:
#   ./scripts/submit.sh                               # Use default config
#   ./scripts/submit.sh 411325471 --max-notes 50      # Override max_notes
#   ./scripts/submit.sh --status 660265
#   ./scripts/submit.sh --type ebay_store villagediecast

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

# Load .env for API port
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
fi

API_PORT="${SCRAPER_API_PORT:-8000}"
API_BASE="http://localhost:$API_PORT"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ─── Check API ───────────────────────────────────────────────

_check_api() {
    if ! curl -sf "$API_BASE/health" >/dev/null 2>&1; then
        echo -e "${RED}API not running on port $API_PORT${NC}"
        echo "Start services first: ./scripts/run.sh start"
        exit 1
    fi
}

# ─── Submit task ─────────────────────────────────────────────

submit_task() {
    local user_id="$1"
    local priority="${PRIORITY:-0}"

    # Build payload JSON from PAYLOAD_CONF associative array
    local payload="{"
    local first=true
    for key in "${!PAYLOAD_CONF[@]}"; do
        local val="${PAYLOAD_CONF[$key]}"
        if [ "$first" = true ]; then first=false; else payload="$payload, "; fi
        # Numeric/boolean values without quotes, strings with quotes
        if [[ "$val" =~ ^[0-9]+$ ]] || [[ "$val" == "true" ]] || [[ "$val" == "false" ]]; then
            payload="$payload\"$key\": $val"
        else
            payload="$payload\"$key\": \"$val\""
        fi
    done
    payload="$payload}"

    local body
    body=$(cat <<EOF
{
    "task_type": "$TASK_TYPE",
    "task_key": "$user_id",
    "priority": $priority,
    "payload": $payload
}
EOF
)

    local resp
    resp=$(curl -s -X POST "$API_BASE/tasks" \
        -H 'Content-Type: application/json' \
        -d "$body")

    # Check for error
    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'id' in d else 1)" 2>/dev/null; then
        local task_id
        task_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
        echo -e "${GREEN}[OK]${NC} Task #$task_id submitted: ${BOLD}$user_id${NC}"
    else
        local detail
        detail=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('detail','Unknown error'))" 2>/dev/null || echo "$resp")
        echo -e "${YELLOW}[SKIP]${NC} $user_id: $detail"
    fi
}

# ─── Task status ─────────────────────────────────────────────

show_status() {
    local task_id="$1"
    local resp
    resp=$(curl -s "$API_BASE/tasks/$task_id")

    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'id' in d else 1)" 2>/dev/null; then
        echo "$resp" | python3 -c "
import sys, json
t = json.load(sys.stdin)
status_colors = {'completed': '\033[92m', 'failed': '\033[91m', 'processing': '\033[93m', 'pending': '\033[36m', 'login_required': '\033[95m'}
sc = status_colors.get(t['status'], '')
print(f\"Task #{t['id']}: {sc}{t['status']}\033[0m\")
print(f\"  Type:     {t['task_type']}/{t['task_key']}\")
print(f\"  Attempts: {t['attempts']}/{t['max_attempts']}\")
if t.get('last_error'): print(f\"  Error:    {t['last_error']}\")
if t.get('result'):
    r = t['result']
    if r.get('notes_count'): print(f\"  Notes:    {r['notes_count']}\")
    if r.get('duration_seconds'): print(f\"  Duration: {r['duration_seconds']:.1f}s\")
"
    else
        echo -e "${RED}Task $task_id not found${NC}"
    fi
}

# ─── List tasks ──────────────────────────────────────────────

list_tasks() {
    local status="${1:-}"
    local url="$API_BASE/tasks?task_type=$TASK_TYPE&limit=20"
    if [ -n "$status" ]; then
        url="$url&status=$status"
    fi

    curl -s "$url" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
if not tasks:
    print('No tasks found')
    sys.exit()
status_colors = {'completed': '\033[92m', 'failed': '\033[91m', 'processing': '\033[93m', 'pending': '\033[36m', 'login_required': '\033[95m', 'claimed': '\033[93m'}
print(f\"{'ID':>8}  {'Status':<18}  {'Key':<30}  {'Attempts':>8}\")
print('-' * 70)
for t in tasks:
    sc = status_colors.get(t['status'], '')
    print(f\"{t['id']:>8}  {sc}{t['status']:<18}\033[0m  {t['task_key']:<30}  {t['attempts']:>3}/{t['max_attempts']}\")
" 2>/dev/null || echo "Failed to list tasks"
}

# ─── Parse config file ───────────────────────────────────────

declare -A PAYLOAD_CONF

_load_conf() {
    local filepath="$1"
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"          # strip comments
        line="${line// /}"          # strip whitespace
        [ -z "$line" ] && continue
        if [[ "$line" == *=* ]]; then
            local key="${line%%=*}"
            local val="${line#*=}"
            PAYLOAD_CONF[$key]="$val"
        else
            USER_IDS+=("$line")
        fi
    done < "$filepath"
}

# ─── Main ────────────────────────────────────────────────────

PRIORITY=0
USER_IDS=()
ACTION="submit"
CONF_LOADED=false
TASK_TYPE="xiaohongshu"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)
            ACTION="status"
            shift
            if [[ $# -gt 0 ]]; then USER_IDS+=("$1"); shift; fi
            ;;
        --list)
            ACTION="list"
            shift
            if [[ $# -gt 0 ]]; then USER_IDS+=("$1"); shift; fi
            ;;
        --file|-f)
            shift
            _load_conf "${1:?--file requires a path}"
            CONF_LOADED=true
            shift
            ;;
        --type|-t)
            shift; TASK_TYPE="${1:?--type requires a value}"; shift ;;
        --max-notes)
            shift; PAYLOAD_CONF[max_notes]="${1:-0}"; shift ;;
        --priority)
            shift; PRIORITY="${1:-0}"; shift ;;
        --help|-h)
            echo "Usage:"
            echo "  $0                                  Submit from config/xhs.conf (default)"
            echo "  $0 <user_id> [user_id ...]          Submit specific users"
            echo "  $0 --file config/xhs.conf            Submit from config file"
            echo "  $0 --status <task_id>                Check task status"
            echo "  $0 --list [status]                   List tasks (optional status filter)"
            echo ""
            echo "Options:"
            echo "  --file, -f FILE   Config file (key=value for payload, other lines are user IDs)"
            echo "  --max-notes N     Override max notes per user"
            echo "  --priority N      Task priority (default: 0, higher = first)"
            echo ""
            echo "Config file format (config/xhs.conf):"
            echo "  skip_existing_days=30"
            echo "  max_notes=0"
            echo "  411325471          # user ID"
            exit 0
            ;;
        *)
            USER_IDS+=("$1"); shift ;;
    esac
done

# Default: if no file specified and no user IDs given, load the config file
# matching the task type (xhs.conf for xiaohongshu, ebay.conf for ebay_store).
if [ ${#USER_IDS[@]} -eq 0 ] && [ "$CONF_LOADED" = false ] && [ "$ACTION" = "submit" ]; then
    case "$TASK_TYPE" in
        xiaohongshu) default_conf="$PROJECT_DIR/config/xhs.conf" ;;
        ebay_store)  default_conf="$PROJECT_DIR/config/ebay.conf" ;;
        *)           default_conf="" ;;
    esac
    if [ -n "$default_conf" ] && [ -f "$default_conf" ]; then
        _load_conf "$default_conf"
    fi
fi

# xiaohongshu-specific default payload
if [ "$TASK_TYPE" = "xiaohongshu" ] && [ -z "${PAYLOAD_CONF[scrape_mode]:-}" ]; then
    PAYLOAD_CONF[scrape_mode]="notes"
fi

_check_api

case "$ACTION" in
    submit)
        if [ ${#USER_IDS[@]} -eq 0 ]; then
            echo "No user IDs found. Add users to config/xhs.conf or pass as arguments."
            echo "Usage: $0 [user_id ...] [--file config/xhs.conf] [--max-notes N]"
            exit 1
        fi
        for uid in "${USER_IDS[@]}"; do
            submit_task "$uid"
        done
        ;;
    status)
        if [ ${#USER_IDS[@]} -eq 0 ]; then
            echo "Usage: $0 --status <task_id>"
            exit 1
        fi
        show_status "${USER_IDS[0]}"
        ;;
    list)
        list_tasks "${USER_IDS[0]:-}"
        ;;
esac
