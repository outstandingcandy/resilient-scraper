#!/usr/bin/env bash
#
# Run resilient-scraper services on EC2 (without Docker).
#
# Usage:
#   ./scripts/run.sh start     # Start Xvfb + Chrome + API + Worker
#   ./scripts/run.sh stop      # Stop all services
#   ./scripts/run.sh restart   # Stop then start
#   ./scripts/run.sh status    # Show running services
#   ./scripts/run.sh logs      # Tail worker logs
#   ./scripts/run.sh stats     # Query task progress (via VPC peering)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
DATA_DIR="$PROJECT_DIR/data"
PIDFILE_DIR="$PROJECT_DIR/.pids"
ENV_FILE="$PROJECT_DIR/.env"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

CHROME_PORT=9222
DISPLAY_NUM=99

# ─── Helpers ─────────────────────────────────────────────────

_load_env() {
    if [ -f "$ENV_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$ENV_FILE"
        set +a
    else
        log_error ".env file not found at $ENV_FILE"
        exit 1
    fi
}

_save_pid() {
    local name="$1" pid="$2"
    mkdir -p "$PIDFILE_DIR"
    echo "$pid" > "$PIDFILE_DIR/$name.pid"
}

_read_pid() {
    local name="$1"
    local pidfile="$PIDFILE_DIR/$name.pid"
    if [ -f "$pidfile" ]; then
        cat "$pidfile"
    fi
}

_is_running() {
    local pid="$1"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

_stop_service() {
    local name="$1"
    local pid
    pid=$(_read_pid "$name")
    if _is_running "$pid"; then
        kill "$pid" 2>/dev/null
        sleep 1
        if _is_running "$pid"; then
            kill -9 "$pid" 2>/dev/null
        fi
        log_info "Stopped $name (pid $pid)"
    fi
    rm -f "$PIDFILE_DIR/$name.pid"
}

_find_chrome() {
    for bin in chromium chromium-browser google-chrome google-chrome-stable; do
        if command -v "$bin" &>/dev/null; then
            echo "$bin"
            return
        fi
    done
}

_check_port() {
    local port="$1"
    curl -sf "http://127.0.0.1:$port" >/dev/null 2>&1 || \
    curl -sf "http://127.0.0.1:$port/json/version" >/dev/null 2>&1
}

# ─── Start ───────────────────────────────────────────────────

cmd_start() {
    _load_env
    mkdir -p "$LOG_DIR" "$DATA_DIR"

    log_info "Starting resilient-scraper services..."

    # 1. Xvfb (skip if --api-only)
    if [ "${API_ONLY:-}" = "1" ]; then
        log_info "Xvfb skipped (--api-only)"
    elif [ -z "${DISPLAY:-}" ]; then
        if command -v Xvfb &>/dev/null; then
            # Check if any Xvfb is already running (e.g. started by another user)
            local existing_display
            existing_display=$(pgrep -a Xvfb 2>/dev/null | grep -oP ':\d+' | head -1 || true)
            if [ -n "$existing_display" ]; then
                export DISPLAY="$existing_display"
                log_ok "Reusing existing Xvfb on $DISPLAY"
            elif ! _is_running "$(_read_pid xvfb)"; then
                Xvfb ":$DISPLAY_NUM" -screen 0 1920x1080x24 \
                    > "$LOG_DIR/xvfb.log" 2>&1 &
                _save_pid xvfb $!
                sleep 0.5
                if _is_running "$!"; then
                    log_ok "Xvfb started on :$DISPLAY_NUM"
                else
                    log_warn "Xvfb failed to start, check $LOG_DIR/xvfb.log"
                fi
                export DISPLAY=":$DISPLAY_NUM"
            else
                log_ok "Xvfb already running (managed)"
                export DISPLAY=":$DISPLAY_NUM"
            fi
        else
            log_warn "Xvfb not found, Chrome may fail"
        fi
    else
        log_ok "DISPLAY=$DISPLAY (existing)"
    fi

    # 2. Chrome (skip if --api-only)
    if [ "${API_ONLY:-}" = "1" ]; then
        log_info "Chrome skipped (--api-only)"
    elif ! _check_port "$CHROME_PORT"; then
        local chrome_bin
        chrome_bin=$(_find_chrome)
        if [ -z "$chrome_bin" ]; then
            log_error "Chrome/Chromium not found"
            exit 1
        fi

        local chrome_data="$DATA_DIR/chrome-profile"
        mkdir -p "$chrome_data"

        "$chrome_bin" \
            --no-sandbox \
            --disable-gpu \
            --disable-dev-shm-usage \
            --remote-debugging-port="$CHROME_PORT" \
            --remote-debugging-address=127.0.0.1 \
            --no-first-run \
            --disable-default-apps \
            --window-size=1920,1080 \
            --user-data-dir="$chrome_data" \
            "about:blank" \
            > "$LOG_DIR/chrome.log" 2>&1 &
        _save_pid chrome $!
        sleep 2

        if _check_port "$CHROME_PORT"; then
            log_ok "Chrome started (CDP port $CHROME_PORT)"
        else
            log_error "Chrome failed to start, check $LOG_DIR/chrome.log"
            exit 1
        fi
    else
        log_ok "Chrome already running on port $CHROME_PORT"
    fi

    # 3. API
    if ! _is_running "$(_read_pid api)"; then
        cd "$PROJECT_DIR"
        nohup uv run python -m resilient_scraper.service.api \
            > "$LOG_DIR/api.log" 2>&1 &
        _save_pid api $!
        log_ok "API started (port ${SCRAPER_API_PORT:-8000})"
    else
        log_ok "API already running"
    fi

    # 4. Worker (skip if --api-only)
    if [ "${API_ONLY:-}" != "1" ]; then
        if ! _is_running "$(_read_pid worker)"; then
            cd "$PROJECT_DIR"
            nohup uv run python -m resilient_scraper.service.worker \
                > "$LOG_DIR/worker.log" 2>&1 &
            _save_pid worker $!
            log_ok "Worker started"
        else
            log_ok "Worker already running"
        fi
    else
        log_info "Worker skipped (--api-only)"
    fi

    sleep 2
    echo ""
    log_ok "All services running!"
    echo ""
    echo -e "  API:     ${BOLD}http://localhost:${SCRAPER_API_PORT:-8000}${NC}"
    if [ "${API_ONLY:-}" != "1" ]; then
        echo -e "  Chrome:  CDP port $CHROME_PORT"
    fi
    echo -e "  Logs:    $LOG_DIR/"
    echo ""
    echo "  ./scripts/run.sh logs      # tail worker logs"
    echo "  ./scripts/run.sh status    # check services"
    echo "  ./scripts/run.sh stop      # stop all"
}

# ─── Stop ────────────────────────────────────────────────────

cmd_stop() {
    log_info "Stopping services..."
    _stop_service worker
    _stop_service api
    _stop_service chrome
    _stop_service xvfb
    # Clean up any orphans
    pkill -f 'resilient_scraper.service.worker' 2>/dev/null || true
    pkill -f 'resilient_scraper.service.api' 2>/dev/null || true
    log_ok "All services stopped"
}

# ─── Status ──────────────────────────────────────────────────

cmd_status() {
    _load_env 2>/dev/null || true
    echo -e "${BOLD}Service status:${NC}"
    for svc in xvfb chrome api worker; do
        local pid
        pid=$(_read_pid "$svc")
        if _is_running "$pid"; then
            echo -e "  $svc: ${GREEN}running${NC} (pid $pid)"
        else
            # Fallback: detect processes not started by our script
            case "$svc" in
                xvfb)
                    local xvfb_pid
                    xvfb_pid=$(pgrep -x Xvfb 2>/dev/null | head -1 || true)
                    if [ -n "$xvfb_pid" ]; then
                        echo -e "  $svc: ${GREEN}running${NC} (pid $xvfb_pid, external)"
                    else
                        echo -e "  $svc: ${RED}stopped${NC}"
                    fi
                    ;;
                chrome)
                    if _check_port "$CHROME_PORT"; then
                        local chrome_pid
                        chrome_pid=$(pgrep -f 'remote-debugging-port='"$CHROME_PORT" 2>/dev/null | head -1 || true)
                        echo -e "  $svc: ${GREEN}running${NC} (port $CHROME_PORT${chrome_pid:+, pid $chrome_pid, external})"
                    else
                        echo -e "  $svc: ${RED}stopped${NC}"
                    fi
                    ;;
                *)
                    echo -e "  $svc: ${RED}stopped${NC}"
                    ;;
            esac
        fi
    done

    echo ""
    local api_port="${SCRAPER_API_PORT:-8000}"
    if curl -sf "http://localhost:$api_port/health" >/dev/null 2>&1; then
        echo -e "${BOLD}API health:${NC}"
        curl -s "http://localhost:$api_port/health" | python3 -m json.tool 2>/dev/null || true
        echo ""
        echo -e "${BOLD}Queue stats:${NC}"
        curl -s "http://localhost:$api_port/stats" | python3 -m json.tool 2>/dev/null || true
    fi
}

# ─── Stats (query DB via VPC peering) ────────────────────────

cmd_stats() {
    _load_env

    # Convert asyncpg URL to psycopg2 format
    local db_url="${DB_URL/postgresql+asyncpg/postgresql}"

    python3 -c "
import sys
try:
    import psycopg2
except ImportError:
    sys.exit('psycopg2 not installed. Run: pip install psycopg2-binary')

from datetime import datetime, timezone

conn = psycopg2.connect('$db_url', connect_timeout=10)
cur = conn.cursor()

# Status summary
cur.execute('SELECT status, COUNT(*) FROM scraper_tasks GROUP BY status ORDER BY count DESC')
rows = cur.fetchall()
total = sum(r[1] for r in rows)
print('\033[1m=== 任务状态统计 ===\033[0m')
for s, c in rows:
    pct = c * 100 // total if total else 0
    bar = '█' * (pct // 2)
    print(f'  {s:20s} {c:>6d}  {pct:>3d}%  {bar}')
print(f'  {\"─\" * 40}')
print(f'  {\"总计\":20s} {total:>6d}')

# By type
cur.execute('SELECT task_type, status, COUNT(*) FROM scraper_tasks GROUP BY task_type, status ORDER BY task_type, count DESC')
print()
print('\033[1m=== 按抓取类型 ===\033[0m')
for r in cur.fetchall():
    print(f'  {r[0]:20s} {r[1]:20s} {r[2]}')

# Workers
cur.execute(\"\"\"SELECT worker_id, status, tasks_completed, last_heartbeat, current_task_id
    FROM scraper_workers ORDER BY last_heartbeat DESC LIMIT 10\"\"\")
rows = cur.fetchall()
print()
print('\033[1m=== Workers ===\033[0m')
if not rows:
    print('  (无 worker 记录)')
for r in rows:
    age = ''
    if r[3]:
        delta = datetime.now(timezone.utc) - r[3]
        mins = int(delta.total_seconds() // 60)
        age = f'{mins}m ago' if mins < 60 else f'{mins // 60}h{mins % 60}m ago'
    print(f'  {r[0]}  status={r[1]}  done={r[2]}  heartbeat={age}  task={r[4]}')

# Recent completions
cur.execute(\"\"\"SELECT id, task_type, task_key, completed_at FROM scraper_tasks
    WHERE status='completed' ORDER BY completed_at DESC LIMIT 5\"\"\")
rows = cur.fetchall()
print()
print('\033[1m=== 最近完成 ===\033[0m')
if not rows:
    print('  (无)')
for r in rows:
    print(f'  #{r[0]:<6d} {r[1]:15s} {str(r[2])[:45]:45s} {r[3]}')

# Recent failures
cur.execute(\"\"\"SELECT id, task_type, task_key, attempts, max_attempts, left(last_error, 100)
    FROM scraper_tasks WHERE status='failed' ORDER BY id DESC LIMIT 5\"\"\")
rows = cur.fetchall()
print()
print('\033[1m=== 最近失败 ===\033[0m')
if not rows:
    print('  (无)')
for r in rows:
    print(f'  #{r[0]:<6d} {r[1]:15s} {str(r[2])[:35]:35s} {r[3]}/{r[4]}  {r[5] or \"\"}')

conn.close()
"
}

# ─── Logs ────────────────────────────────────────────────────

cmd_logs() {
    local target="${1:-worker}"
    local logfile="$LOG_DIR/$target.log"
    if [ -f "$logfile" ]; then
        tail -f "$logfile"
    else
        log_error "Log file not found: $logfile"
        echo "Available: $(ls "$LOG_DIR"/*.log 2>/dev/null | xargs -n1 basename | tr '\n' ' ')"
    fi
}

# ─── Main ────────────────────────────────────────────────────

case "${1:-}" in
    start)        cmd_start ;;
    start-api)    API_ONLY=1 cmd_start ;;
    stop)         cmd_stop ;;
    restart)      cmd_stop; sleep 1; cmd_start ;;
    restart-api)  cmd_stop; sleep 1; API_ONLY=1 cmd_start ;;
    status)       cmd_status ;;
    logs)         cmd_logs "${2:-worker}" ;;
    stats)        cmd_stats ;;
    *)
        echo "Usage: $0 {start|start-api|stop|restart|restart-api|status|logs|stats}"
        exit 1
        ;;
esac
