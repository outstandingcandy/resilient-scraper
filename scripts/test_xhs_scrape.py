#!/usr/bin/env python3
"""End-to-end test script for Xiaohongshu scraping via the service API.

Prerequisites:
    - Chrome running with remote debugging:
        google-chrome --remote-debugging-port=9222
    - PostgreSQL running (external, provide via --db-url or DB_URL env var)

Usage:
    # Run the test (starts API + Worker, connects to your Chrome)
    python scripts/test_xhs_scrape.py <user_id> --db-url "postgresql+asyncpg://..." [--max-notes 5]

    # With S3 upload
    python scripts/test_xhs_scrape.py <user_id> --db-url "..." --s3-bucket my-bucket --s3-prefix xiaohongshu

    # Custom Chrome debug port
    python scripts/test_xhs_scrape.py <user_id> --db-url "..." --chrome-port 9222

    # If API/Worker already running
    python scripts/test_xhs_scrape.py <user_id> --api-only

The script will:
    - Start the API server and Worker process
    - Worker connects to your real Chrome browser via CDP (port 9222)
    - Submit a scraping task for the given XHS user
    - Poll task status and display progress
    - When login is required, save the QR screenshot locally and open it
    - Wait for you to scan the QR code
    - Show final results when scraping completes
"""

import argparse
import base64
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

API_BASE = "http://localhost:18000"

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def log(msg: str, color: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{CYAN}[{ts}]{RESET} {color}{msg}{RESET}")


def api_get(path: str) -> dict | bytes | None:
    """GET request to API, returns parsed JSON or raw bytes."""
    try:
        req = Request(f"{API_BASE}{path}")
        with urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
            if "image/" in content_type:
                return data
            return json.loads(data)
    except URLError as e:
        if hasattr(e, "code") and e.code == 404:
            return None
        raise


def api_post(path: str, body: dict) -> dict:
    """POST JSON to API."""
    data = json.dumps(body).encode()
    req = Request(f"{API_BASE}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def wait_for_api(timeout: int = 30) -> bool:
    """Wait for API to become healthy."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = api_get("/health")
            if resp and resp.get("status") == "healthy":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _check_chrome_running(port: int) -> bool:
    """Check if Chrome CDP is reachable on the given port."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _find_chrome() -> str | None:
    """Find Chrome/Chromium binary."""
    import shutil
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def open_image(filepath: str) -> None:
    """Try to open/display an image file."""
    # Try common image viewers
    for cmd in ["xdg-open", "open", "eog", "feh", "display"]:
        try:
            subprocess.Popen(
                [cmd, filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except FileNotFoundError:
            continue

    # Fallback: print as base64 data URL (works in some terminals)
    log(f"Cannot open image viewer. Screenshot saved to: {filepath}", YELLOW)
    log("If using iTerm2/kitty, the image may display inline below:", YELLOW)
    try:
        with open(filepath, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        # iTerm2 inline image protocol
        print(f"\033]1337;File=inline=1;width=40;preserveAspectRatio=1:{b64}\a")
    except Exception:
        pass


def fetch_screenshot(task_id: int) -> tuple[bytes | None, str | None]:
    """Fetch screenshot from API, handling both S3 URL and BLOB responses.

    Returns:
        Tuple of (screenshot_bytes, screenshot_url).
        One of them will be set, the other None.
    """
    resp = api_get(f"/tasks/{task_id}/screenshot")
    if resp is None:
        return None, None

    # New format: JSON with S3 URL
    if isinstance(resp, dict) and "url" in resp:
        s3_url = resp["url"]
        try:
            with urlopen(s3_url, timeout=15) as s3_resp:
                return s3_resp.read(), s3_url
        except Exception:
            return None, s3_url

    # Legacy format: raw bytes
    if isinstance(resp, bytes):
        return resp, None

    return None, None


def save_screenshot_locally(data: bytes, name: str) -> str:
    """Save screenshot bytes to local file, return path."""
    script_dir = Path(__file__).resolve().parent.parent
    screenshots_dir = script_dir / "data" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    filepath = str(screenshots_dir / name)
    with open(filepath, "wb") as f:
        f.write(data)
    return filepath


def main() -> None:
    parser = argparse.ArgumentParser(description="Test XHS scraping via service API")
    parser.add_argument("user_id", help="Xiaohongshu user ID to scrape")
    parser.add_argument("--max-notes", type=int, default=5, help="Max notes to scrape (default: 5)")
    parser.add_argument("--nickname", default="", help="Display name for logging")
    parser.add_argument("--api-only", action="store_true", help="Don't start API/Worker (assume already running)")
    parser.add_argument("--chrome-port", type=int, default=9222, help="Chrome CDP debug port (default: 9222)")
    parser.add_argument("--poll-interval", type=int, default=3, help="Status poll interval in seconds")
    parser.add_argument("--login-timeout", type=int, default=300, help="Login timeout in seconds (default: 300)")
    parser.add_argument("--db-url", default=os.environ.get("DB_URL", ""),
                        help="Database URL (default: DB_URL env var)")
    parser.add_argument("--s3-bucket", default=os.environ.get("S3_BUCKET", ""),
                        help="S3 bucket for raw data upload (default: S3_BUCKET env var)")
    parser.add_argument("--s3-prefix", default=os.environ.get("S3_PREFIX", ""),
                        help="S3 key prefix (default: S3_PREFIX env var)")
    args = parser.parse_args()

    if not args.db_url and not args.api_only:
        log("Error: --db-url or DB_URL env var is required", RED)
        log("Example: python scripts/test_xhs_scrape.py <user_id> --db-url 'postgresql+asyncpg://postgres:postgres@localhost:5432/scraper'", YELLOW)
        sys.exit(1)

    processes: list[subprocess.Popen] = []
    screenshot_path: str | None = None

    def cleanup(sig=None, frame=None):
        log("Cleaning up...", YELLOW)
        for p in processes:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                p.kill()
        if sig:
            sys.exit(1)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    env = {
        **os.environ,
        "DB_URL": args.db_url,
        "SCRAPER_API_PORT": "18000",
        "SCRAPER_LOG_LEVEL": "INFO",
        # External browser mode: connect to user's real Chrome
        "BROWSER_CHROME_DEBUG_PORT": str(args.chrome_port),
        "WORKER_TASK_TIMEOUT": str(args.login_timeout + 600),  # login timeout + scrape time
    }
    if args.s3_bucket:
        env["S3_BUCKET"] = args.s3_bucket
    if args.s3_prefix:
        env["S3_PREFIX"] = args.s3_prefix

    try:
        if not args.api_only:
            # --- Kill residual processes from previous runs ---
            for proc_name in ("resilient_scraper.service.api", "resilient_scraper.service.worker"):
                subprocess.run(
                    ["pkill", "-f", proc_name],
                    capture_output=True,
                )
            time.sleep(1)

            # --- Start Xvfb if no display ---
            if not os.environ.get("DISPLAY"):
                import shutil
                if shutil.which("Xvfb"):
                    log("No DISPLAY set, starting Xvfb...", YELLOW)
                    # Find a free display number to avoid conflicts with stale sockets
                    xvfb_display = None
                    for display_num in range(99, 200):
                        lock_file = f"/tmp/.X{display_num}-lock"
                        socket_file = f"/tmp/.X11-unix/X{display_num}"
                        if not os.path.exists(lock_file) and not os.path.exists(socket_file):
                            # Try starting Xvfb on this display
                            test_proc = subprocess.Popen(
                                ["Xvfb", f":{display_num}", "-screen", "0", "1920x1080x24"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            time.sleep(1)
                            if test_proc.poll() is None:
                                # Successfully started
                                xvfb_display = display_num
                                xvfb_proc = test_proc
                                processes.append(xvfb_proc)
                                break
                            # Failed (e.g., abstract socket conflict), try next
                            test_proc.kill()
                            test_proc.wait()
                        else:
                            # Lock or socket file exists, clean up and skip
                            continue

                    if xvfb_display is None:
                        log("Could not find a free display for Xvfb!", RED)
                        cleanup()
                        return

                    os.environ["DISPLAY"] = f":{xvfb_display}"
                    env["DISPLAY"] = f":{xvfb_display}"
                    log(f"Xvfb ready on :{xvfb_display}", GREEN)
                else:
                    log("No DISPLAY and Xvfb not found. Chrome may fail to start.", YELLOW)

            # --- Start Chrome with remote debugging ---
            chrome_port = args.chrome_port
            if not _check_chrome_running(chrome_port):
                log(f"Starting Chrome with --remote-debugging-port={chrome_port}...", BOLD)
                chrome_bin = _find_chrome()
                if not chrome_bin:
                    log("Chrome/Chromium not found! Install it or start manually.", RED)
                    return

                chrome_data_dir = str(Path(__file__).resolve().parent.parent / "data" / "chrome-profile")
                os.makedirs(chrome_data_dir, exist_ok=True)

                chrome_proc = subprocess.Popen(
                    [chrome_bin,
                     "--no-sandbox",
                     "--disable-dev-shm-usage",
                     "--disable-gpu",
                     f"--remote-debugging-port={chrome_port}",
                     "--remote-debugging-address=127.0.0.1",
                     f"--user-data-dir={chrome_data_dir}",
                     "--no-first-run",
                     "--disable-default-apps",
                     "--window-size=1920,1080",
                     "about:blank"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                processes.append(chrome_proc)
                time.sleep(3)

                if not _check_chrome_running(chrome_port):
                    log(f"Chrome failed to start on port {chrome_port}!", RED)
                    cleanup()
                    return

                log(f"Chrome ready (CDP port {chrome_port})", GREEN)
            else:
                log(f"Chrome already running on port {chrome_port}", GREEN)

            # --- Start API ---
            log("Starting API server on port 18000...", BOLD)
            api_proc = subprocess.Popen(
                [sys.executable, "-m", "resilient_scraper.service.api"],
                env=env,
                stdout=open("/tmp/scraper-api-test.log", "w"),
                stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            processes.append(api_proc)

            if not wait_for_api():
                log("API failed to start! Check /tmp/scraper-api-test.log", RED)
                cleanup()
                return

            log(f"API ready at {API_BASE}", GREEN)

            # --- Clean up stale tasks BEFORE starting worker ---
            # Worker's claim_task resets stale tasks to 'pending' and claims
            # them immediately, so we must cancel them before worker starts.
            try:
                existing = api_get(f"/tasks?task_type=xiaohongshu&limit=200")
                if existing:
                    from urllib.request import Request as Req
                    for t in existing:
                        if t["task_key"] == args.user_id and t["status"] not in ("completed", "failed", "no_data"):
                            log(f"Cancelling stale task {t['id']} (status: {t['status']})", YELLOW)
                            try:
                                cancel_req = Req(f"{API_BASE}/tasks/{t['id']}", method="DELETE")
                                urlopen(cancel_req, timeout=5)
                            except Exception:
                                pass
            except Exception:
                pass

            # --- Start Worker ---
            log(f"Starting Worker (connecting to Chrome on port {chrome_port})...", BOLD)
            worker_proc = subprocess.Popen(
                [sys.executable, "-m", "resilient_scraper.service.worker"],
                env=env,
                stdout=open("/tmp/scraper-worker-test.log", "w"),
                stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            processes.append(worker_proc)
            time.sleep(3)  # Let worker initialize

            if worker_proc.poll() is not None:
                log("Worker failed to start! Check /tmp/scraper-worker-test.log", RED)
                cleanup()
                return

            log("Worker ready", GREEN)
        else:
            log("Using existing API/Worker (--api-only)", YELLOW)
            if not wait_for_api(timeout=5):
                log(f"API not reachable at {API_BASE}", RED)
                return

        # --- Submit task ---
        payload = {"max_notes": args.max_notes}
        if args.nickname:
            payload["nickname"] = args.nickname

        log(f"Submitting XHS scrape task: user_id={args.user_id}, max_notes={args.max_notes}", BOLD)

        resp = api_post("/tasks", {
            "task_type": "xiaohongshu",
            "task_key": args.user_id,
            "payload": payload,
        })

        task_id = resp["id"]
        log(f"Task created: id={task_id}", GREEN)

        # --- Poll for status ---
        log("Polling task status...", BOLD)
        print()

        last_status = None
        screenshot_shown = False
        last_screenshot_size = 0
        sms_submitted = False
        start_time = time.time()
        worker_log_pos = 0  # Track position in worker log for streaming

        while True:
            task = api_get(f"/tasks/{task_id}")
            if not task:
                log("Task not found!", RED)
                break

            status = task["status"]
            elapsed = time.time() - start_time

            if status != last_status:
                status_colors = {
                    "pending": YELLOW,
                    "claimed": CYAN,
                    "processing": CYAN,
                    "login_required": f"{BOLD}{YELLOW}",
                    "completed": GREEN,
                    "failed": RED,
                    "no_data": YELLOW,
                }
                color = status_colors.get(status, "")
                log(f"Status: {color}{status}{RESET}  (elapsed: {elapsed:.0f}s, attempts: {task['attempts']})")
                last_status = status

            # --- Fetch latest screenshot during processing ---
            if status in ("processing", "claimed"):
                try:
                    ss_data, ss_url = fetch_screenshot(task_id)
                    if ss_data:
                        current_size = len(ss_data)
                        if current_size != last_screenshot_size:
                            last_screenshot_size = current_size
                            screenshot_path = save_screenshot_locally(ss_data, f"xhs_progress_{task_id}.png")
                            log(f"Screenshot: {screenshot_path}", CYAN)
                    elif ss_url:
                        log(f"Screenshot URL: {ss_url}", CYAN)
                except Exception:
                    pass  # screenshot not available yet

            # --- Handle login_required ---
            if status == "login_required":
                ss_data, ss_url = fetch_screenshot(task_id)
                if ss_data:
                    # Only update file and print when screenshot content changed
                    current_size = len(ss_data)
                    screenshot_changed = current_size != last_screenshot_size

                    if screenshot_changed:
                        last_screenshot_size = current_size
                        screenshot_path = save_screenshot_locally(ss_data, f"xhs_qr_{task_id}.png")

                        if not screenshot_shown:
                            screenshot_shown = True
                            log("Please scan the QR code with your Xiaohongshu app!", f"{BOLD}{YELLOW}")
                        else:
                            log("Screenshot updated (page refreshed)", YELLOW)

                        log(f"Screenshot: {screenshot_path}", YELLOW)
                        if ss_url:
                            log(f"S3 URL:     {ss_url}", YELLOW)

                    login_phase = task.get("last_error", "qr_scan")
                    if login_phase == "sms_verification":
                        if sms_submitted:
                            print(f"{CYAN}[SMS]{RESET}  Waiting for verification... ", end="", flush=True)
                            ready, _, _ = select.select([sys.stdin], [], [], args.poll_interval)
                            if ready:
                                code = sys.stdin.readline().strip()
                                if code:
                                    sms_submitted = False  # allow re-prompt for new code
                            else:
                                print()
                        else:
                            print(f"{BOLD}{YELLOW}[SMS]{RESET} Enter verification code: ", end="", flush=True)
                            ready, _, _ = select.select([sys.stdin], [], [], args.poll_interval)
                            if ready:
                                code = sys.stdin.readline().strip()
                                if code:
                                    log(f"Submitting SMS code: {code}", BOLD)
                                    try:
                                        api_post(f"/tasks/{task_id}/input", {"value": code})
                                        log("Code submitted, waiting for verification...", GREEN)
                                        sms_submitted = True
                                    except Exception as e:
                                        log(f"Failed to submit code: {e}", RED)
                            else:
                                print()
                    else:
                        sms_submitted = False  # reset if phase changed back to QR
                        print(f"{CYAN}[QR]{RESET}  Waiting for QR scan... ", end="", flush=True)
                        ready, _, _ = select.select([sys.stdin], [], [], args.poll_interval)
                        if ready:
                            sys.stdin.readline()  # discard accidental input during QR phase
                        else:
                            print()
                    continue  # skip the sleep at the bottom
                else:
                    log("No screenshot available yet, will retry...", YELLOW)

            # --- Terminal states ---
            if status == "completed":
                print()
                log("Scraping completed!", f"{BOLD}{GREEN}")
                result = task.get("result") or {}
                log(f"  Notes scraped: {result.get('notes_count', 'N/A')}", GREEN)
                log(f"  Images downloaded: {result.get('images_downloaded', 'N/A')}", GREEN)
                log(f"  Duration: {result.get('duration_seconds', elapsed):.1f}s", GREEN)
                break

            if status == "failed":
                print()
                error = task.get("last_error", "Unknown error")
                attempts = task["attempts"]
                max_attempts = task["max_attempts"]
                if attempts < max_attempts:
                    log(f"Task failed (attempt {attempts}/{max_attempts}): {error}", YELLOW)
                    log("Will be retried automatically...", YELLOW)
                    screenshot_shown = False  # Reset for potential re-login
                    sms_submitted = False
                else:
                    log(f"Task failed permanently after {attempts} attempts: {error}", RED)
                    try:
                        ss_data, ss_url = fetch_screenshot(task_id)
                        if ss_data:
                            final_path = save_screenshot_locally(ss_data, f"xhs_final_{task_id}.png")
                            log(f"Final screenshot: {final_path}", YELLOW)
                        elif ss_url:
                            log(f"Final screenshot URL: {ss_url}", YELLOW)
                    except Exception:
                        pass
                    break

            if status == "no_data":
                print()
                log(f"No data found: {task.get('last_error', 'N/A')}", YELLOW)
                # Fetch final screenshot to show what the scraper last saw
                try:
                    ss_data, ss_url = fetch_screenshot(task_id)
                    if ss_data:
                        final_path = save_screenshot_locally(ss_data, f"xhs_final_{task_id}.png")
                        log(f"Final screenshot: {final_path}", YELLOW)
                    elif ss_url:
                        log(f"Final screenshot URL: {ss_url}", YELLOW)
                except Exception:
                    pass
                break

            # --- Stream new worker log lines ---
            try:
                with open("/tmp/scraper-worker-test.log") as f:
                    f.seek(worker_log_pos)
                    new_lines = f.readlines()
                    worker_log_pos = f.tell()
                for line in new_lines:
                    line = line.rstrip()
                    if not line:
                        continue
                    # Color based on log level
                    if "ERROR:" in line:
                        print(f"  {RED}{line}{RESET}")
                    elif "WARNING:" in line:
                        print(f"  {YELLOW}{line}{RESET}")
                    elif "Screenshot saved:" in line or "Screenshot dir:" in line:
                        print(f"  {CYAN}{line}{RESET}")
                    else:
                        print(f"  {line}")
            except FileNotFoundError:
                pass

            time.sleep(args.poll_interval)

        # --- Show final stats ---
        print()
        stats = api_get("/stats")
        if stats:
            log("Queue stats:", BOLD)
            for key in ("pending", "processing", "completed", "failed", "login_required"):
                val = stats.get(key, 0)
                if val > 0:
                    log(f"  {key}: {val}")

        # --- Show worker logs tail ---
        if not args.api_only:
            print()
            log("Worker log (last 20 lines):", BOLD)
            try:
                with open("/tmp/scraper-worker-test.log") as f:
                    lines = f.readlines()
                    for line in lines[-20:]:
                        print(f"  {line.rstrip()}")
            except FileNotFoundError:
                pass

    except KeyboardInterrupt:
        print()
        log("Interrupted by user", YELLOW)
    except Exception as e:
        log(f"Error: {e}", RED)
        import traceback
        traceback.print_exc()
    finally:
        cleanup()


if __name__ == "__main__":
    main()
