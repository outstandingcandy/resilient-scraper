"""
ResilientScraper — browser-based web scraper with anti-detection capabilities.

Provides a complete abstract base class for scraping pages protected by:
- Login walls (detection, screenshot, email alert, manual wait)
- Cloudflare challenges (detection, wait, screenshot)
- Cookie consent dialogs (auto-dismiss)
- Overlays and modals (auto-close)
- Infinite scroll pagination (multi-strategy loading)
- Browser disconnection (reconnection support)

Also includes S3 upload, debug file saving, and configurable delays.
"""

import json as _json
import logging
import os
import random
import re
import smtplib
import time
from abc import ABC, abstractmethod
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from typing import Any, Generic, TypeVar

from resilient_scraper.errors import ScraperError
from resilient_scraper.models import ScraperResult

logger = logging.getLogger("resilient_scraper")

T = TypeVar("T", bound=ScraperResult)


class ResilientScraper(ABC, Generic[T]):
    """Abstract base class for resilient web scrapers.

    Subclasses must implement:
        - task_type: Class attribute identifying the scraper type.
        - scrape(): Main scraping logic.
        - validate_task(): Task validation logic.

    Subclasses should override class attributes for site-specific behavior:
        - LOGIN_INDICATORS: Text patterns indicating login is required.
        - LOGIN_SELECTORS: CSS/XPath selectors for login form elements.
        - OVERLAY_CLOSE_SELECTORS: Selectors for overlay close buttons.
        - MODAL_CLOSE_SELECTORS: Selectors for modal close buttons.
        - platform_display_name: Human-readable platform name for alerts.

    Configuration (passed via config dict):
        See __init__ for all supported configuration options.
    """

    # --- Class attributes (override in subclasses) ---

    task_type: str = "base"
    default_delay: tuple[float, float] = (5.0, 15.0)
    requires_browser: bool = True
    cloudflare_protected: bool = False
    # Per-scraper task timeout in seconds. 0 means use worker default.
    task_timeout: int = 0

    # Human-readable platform name for email alerts
    platform_display_name: str = ""

    # Site-specific login detection patterns
    LOGIN_INDICATORS: list[str] = []
    LOGIN_SELECTORS: list[str] = []

    # Site-specific overlay/modal close selectors
    OVERLAY_CLOSE_SELECTORS: list[str] = []
    MODAL_CLOSE_SELECTORS: list[str] = []

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the scraper.

        Args:
            config: Configuration dictionary with the following options:

                Basic:
                    screenshots_dir: Debug screenshot directory.
                    local_mode: Skip email notifications.

                S3 Upload:
                    s3_upload: Enable S3 upload (default: False).
                    s3_bucket: S3 bucket name.
                    s3_prefix: S3 key prefix.
                    delete_local_after_upload: Delete local files after S3 upload.

                Database:
                    database_url: SQLAlchemy database URL.

                Browser:
                    use_existing_browser: Connect to existing Chrome (default: False).
                    chrome_debug_port: Chrome debugging port (default: 9222).

                Login Handling:
                    wait_for_login: Wait for manual login (default: True).
                    login_check_interval: Seconds between login checks (default: 5).
                    login_timeout: Max seconds to wait for login (default: 300).

                Email Alerts:
                    login_alert_email: Email to send login alerts to.
                    smtp_server: SMTP server (default: "smtp.qq.com").
                    smtp_port: SMTP port (default: 465).
                    smtp_sender: SMTP sender email.
                    smtp_password: SMTP password.
        """
        self.config = config or {}
        self._setup_complete = False

        # Debug/screenshots
        self.screenshots_dir = self.config.get(
            "screenshots_dir", f"data/{self.task_type}_screenshots"
        )
        self.local_mode = self.config.get("local_mode", False)

        # Per-run screenshot subdirectory and sequential counter
        self._run_screenshots_dir: str | None = None
        self._screenshot_counter: int = 0

        # S3 configuration
        self.s3_enabled = self.config.get("s3_upload", False)
        self.s3_bucket = self.config.get("s3_bucket", "")
        self.s3_prefix = self.config.get("s3_prefix", f"data/{self.task_type}")
        self.delete_local_after_upload = self.config.get(
            "delete_local_after_upload", False
        )
        self.s3_client: Any | None = None

        # Database
        self.database_url = self.config.get("database_url", "")
        self.db_engine: Any | None = None

        # Browser connection
        self.use_existing_browser = self.config.get("use_existing_browser", False)
        self.chrome_debug_port = self.config.get("chrome_debug_port", 9222)
        self._external_browser: Any | None = None

        # Login handling
        self.wait_for_login_enabled = self.config.get("wait_for_login", True)
        self.login_check_interval = self.config.get("login_check_interval", 5)
        self.login_timeout = self.config.get("login_timeout", 300)

        # Email alerts
        self.login_alert_email = self.config.get("login_alert_email", "")
        self.smtp_server = self.config.get("smtp_server", "smtp.qq.com")
        self.smtp_port = self.config.get("smtp_port", 465)
        self.smtp_sender = self.config.get("smtp_sender", "")
        self.smtp_password = self.config.get("smtp_password", "")

        # Cookie persistence
        self.cookies_dir = self.config.get("cookies_dir", "data/cookies")
        self.cookie_max_age_hours = self.config.get("cookie_max_age_hours", 24)

        # Login screenshot callback (set by service worker to store screenshots in DB)
        # Signature: (task_id: int, screenshot_bytes: bytes) -> None
        self.on_login_screenshot: Any | None = None
        # User input callback (set by service worker to poll DB for user-submitted values)
        # Signature: (task_id: int) -> str | None
        self.on_poll_user_input: Any | None = None
        # Login success callback (set by service worker to update task status after login)
        # Signature: (task_id: int) -> None
        self.on_login_success: Any | None = None
        # Page screenshot callback (set by service worker to store debug screenshots in DB)
        # Signature: (task_id: int, screenshot_bytes: bytes) -> None
        self.on_page_screenshot: Any | None = None
        # Alert callback (set by service worker to send login alerts via Feishu etc.)
        # Signature: (task_id: int, context_key: str, screenshot_bytes: bytes | None, phase: str) -> None
        self.on_send_alert: Any | None = None
        self._current_task_id: int | None = None

    # ===================================================================
    # Abstract methods (must be implemented by subclasses)
    # ===================================================================

    @abstractmethod
    def scrape(self, task: Any, browser: Any | None = None) -> T:
        """Execute the scraping operation.

        Args:
            task: The task to process.
            browser: Browser instance (DrissionPage) if requires_browser is True.

        Returns:
            ScraperResult or subclass with extracted data.
        """
        ...

    @abstractmethod
    def validate_task(self, task: Any) -> bool:
        """Validate that a task can be processed by this scraper.

        Args:
            task: The task to validate.

        Returns:
            True if the task is valid, False otherwise.
        """
        ...

    # ===================================================================
    # Lifecycle hooks (override in subclasses as needed)
    # ===================================================================

    def build_url(self, task: Any) -> str:
        """Construct the target URL from task data."""
        return task.payload.get("url", "") if hasattr(task, "payload") else ""

    def parse_response(self, html: str, task: Any) -> dict[str, Any]:
        """Parse HTML response and extract data."""
        return {"raw_html_length": len(html)}

    def post_process(self, data: dict[str, Any], task: Any) -> dict[str, Any]:
        """Transform extracted data before returning."""
        return data

    def setup(self) -> None:
        """Perform setup operations before scraping."""
        self._setup_complete = True
        os.makedirs(self.screenshots_dir, exist_ok=True)

        # Initialize S3 client if enabled
        if self.s3_enabled and self.s3_bucket:
            try:
                import boto3
                self.s3_client = boto3.client("s3")
                logger.info(
                    f"[{self.task_type}] S3 upload enabled: "
                    f"s3://{self.s3_bucket}/{self.s3_prefix}"
                )
            except Exception as e:
                logger.error(f"[{self.task_type}] Failed to init S3 client: {e}")
                self.s3_enabled = False

        # Initialize database engine if configured
        if self.database_url:
            try:
                from sqlalchemy import create_engine
                self.db_engine = create_engine(
                    self.database_url, echo=False, pool_pre_ping=True
                )
                logger.info(f"[{self.task_type}] Database engine initialized")
            except Exception as e:
                logger.error(f"[{self.task_type}] Failed to init DB engine: {e}")

        logger.debug(f"[{self.task_type}] Setup complete")

    def teardown(self) -> None:
        """Perform cleanup operations after scraping."""
        self._setup_complete = False
        logger.debug(f"[{self.task_type}] Teardown complete")

    def on_success(self, task: Any, result: T) -> None:
        """Handle successful scrape completion."""
        logger.info(
            f"[{self.task_type}] Task {task.task_key} completed successfully"
        )

    def on_failure(self, task: Any, error: Exception) -> None:
        """Handle scrape failure."""
        logger.error(f"[{self.task_type}] Task {task.task_key} failed: {error}")

    def should_retry(self, task: Any, error: Exception) -> bool:
        """Determine if a failed task should be retried."""
        return task.attempts < task.max_attempts

    # ===================================================================
    # Delay management
    # ===================================================================

    def get_delay(self) -> float:
        """Get a randomized delay between requests."""
        min_delay, max_delay = self.default_delay
        return random.uniform(min_delay, max_delay)

    def wait_delay(self) -> None:
        """Wait for the configured delay between requests."""
        delay = self.get_delay()
        logger.debug(f"[{self.task_type}] Waiting {delay:.1f}s between requests")
        time.sleep(delay)

    # ===================================================================
    # Login detection and handling
    # ===================================================================

    def _detect_login_required(self, browser: Any) -> bool:
        """Check if login is required to continue.

        Uses LOGIN_INDICATORS (text patterns) and LOGIN_SELECTORS
        (CSS/XPath selectors) defined as class attributes.

        Args:
            browser: DrissionPage browser instance.

        Returns:
            True if login is required, False otherwise.
        """
        try:
            html = browser.html
            page_text = html.lower() if html else ""

            # Check text indicators
            for indicator in self.LOGIN_INDICATORS:
                if indicator.lower() in page_text or indicator in (html or ""):
                    logger.debug(
                        f"[{self.task_type}] Login indicator detected: {indicator}"
                    )
                    return True

            # Check DOM selectors
            for selector in self.LOGIN_SELECTORS:
                try:
                    element = browser.ele(selector, timeout=1)
                    if element:
                        logger.debug(
                            f"[{self.task_type}] Login element found: {selector}"
                        )
                        return True
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.warning(f"[{self.task_type}] Error detecting login: {e}")
            return False

    def _take_login_screenshot(
        self, browser: Any, context_key: str, phase: str = "qr_scan"
    ) -> str | None:
        """Capture screenshot when login is detected.

        Args:
            browser: DrissionPage browser instance.
            context_key: Identifier for the current operation.
            phase: Login phase — "qr_scan" or "sms_verification".

        Returns:
            Path to saved screenshot or None if failed.
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_key = re.sub(r'[^\w\-]', '_', context_key)
            filename = f"login_required_{safe_key}_{timestamp}.png"
            filepath = os.path.join(self.screenshots_dir, filename)

            if hasattr(browser, "get_screenshot"):
                browser.get_screenshot(path=filepath)
            elif hasattr(browser, "save_screenshot"):
                browser.save_screenshot(filepath)

            logger.info(f"[{self.task_type}] Login screenshot saved: {filepath} (phase={phase})")

            # Notify service worker via callback (stores screenshot in DB for API access)
            if self.on_login_screenshot and self._current_task_id and os.path.exists(filepath):
                try:
                    with open(filepath, "rb") as f:
                        self.on_login_screenshot(self._current_task_id, f.read(), phase)
                except Exception as cb_err:
                    logger.warning(f"[{self.task_type}] Login screenshot callback error: {cb_err}")

            return filepath

        except Exception as e:
            logger.error(f"[{self.task_type}] Failed to take login screenshot: {e}")
            return None

    def _send_login_alert(
        self, context_key: str, screenshot_path: str | None, phase: str = "qr_scan"
    ) -> bool:
        """Send alert when login is required.

        Dispatches through on_send_alert callback (Feishu) if set,
        otherwise falls back to SMTP email.

        Args:
            context_key: Identifier for the current operation.
            screenshot_path: Path to screenshot if available.
            phase: Login phase — "qr_scan" or "sms_verification".

        Returns:
            True if alert was sent, False otherwise.
        """
        # Dispatch through callback (Feishu) if available
        if self.on_send_alert and self._current_task_id:
            try:
                screenshot_bytes: bytes | None = None
                if screenshot_path and os.path.exists(screenshot_path):
                    with open(screenshot_path, "rb") as f:
                        screenshot_bytes = f.read()
                self.on_send_alert(self._current_task_id, context_key, screenshot_bytes, phase)
                return True
            except Exception as e:
                logger.warning(f"[{self.task_type}] on_send_alert callback error: {e}")
                # Fall through to email

        if self.local_mode:
            logger.debug(f"[{self.task_type}] Local mode, skipping email alert")
            return False

        if not self.login_alert_email or not self.smtp_sender or not self.smtp_password:
            logger.warning(f"[{self.task_type}] Email alert not configured, skipping")
            return False

        try:
            platform = self.platform_display_name or self.task_type
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            msg = MIMEMultipart()
            msg["From"] = self.smtp_sender
            msg["To"] = self.login_alert_email
            msg["Subject"] = f"[Alert] {platform} Login Required - {context_key}"

            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h2 style="color: #e74c3c;">Login Required Alert</h2>
                <table style="border-collapse: collapse; margin: 20px 0;">
                    <tr>
                        <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Platform</td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{platform}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Context</td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{context_key}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Time</td>
                        <td style="padding: 8px; border: 1px solid #ddd;">{timestamp}</td>
                    </tr>
                </table>
                {"<p><strong>Screenshot attached.</strong></p>" if screenshot_path else ""}
            </body>
            </html>
            """
            msg.attach(MIMEText(html_body, "html"))

            # Attach screenshot if available
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    attachment = MIMEBase("application", "octet-stream")
                    attachment.set_payload(f.read())
                    encoders.encode_base64(attachment)
                    attachment.add_header(
                        "Content-Disposition",
                        f"attachment; filename={os.path.basename(screenshot_path)}",
                    )
                    msg.attach(attachment)

            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port) as server:
                server.login(self.smtp_sender, self.smtp_password)
                server.sendmail(
                    self.smtp_sender, [self.login_alert_email], msg.as_string()
                )

            logger.info(f"[{self.task_type}] Login alert sent to {self.login_alert_email}")
            return True

        except Exception as e:
            logger.error(f"[{self.task_type}] Failed to send login alert: {e}")
            return False

    SMS_VERIFICATION_SELECTORS: list[str] = [
        "xpath://input[@placeholder='Please enter verification code']",
        "xpath://input[@placeholder='请输入验证码']",
        "xpath://input[contains(@placeholder, 'verification')]",
        "xpath://input[contains(@placeholder, '验证码')]",
    ]

    SMS_VERIFICATION_INDICATORS: list[str] = [
        "SMS Verification",
        "短信验证",
        "verification code has been sent",
        "验证码已发送",
    ]

    def _detect_sms_verification(self, browser: Any) -> bool:
        """Check if an SMS verification dialog is showing.

        Requires BOTH a text indicator AND an input field selector to match,
        to avoid false positives on login pages that mention SMS login as an
        option but don't actually have a verification code input field.
        """
        try:
            html = browser.html.lower() if hasattr(browser, "html") else ""
            page_text = browser.text if hasattr(browser, "text") else ""

            has_indicator = False
            for indicator in self.SMS_VERIFICATION_INDICATORS:
                if indicator.lower() in html or indicator.lower() in page_text.lower():
                    has_indicator = True
                    break

            has_input = False
            for selector in self.SMS_VERIFICATION_SELECTORS:
                try:
                    element = browser.ele(selector, timeout=1)
                    if element:
                        has_input = True
                        break
                except Exception:
                    continue

            return has_indicator and has_input
        except Exception:
            return False

    def _fill_sms_code(self, browser: Any, code: str) -> bool:
        """Fill in the SMS verification code and submit."""
        try:
            for selector in self.SMS_VERIFICATION_SELECTORS:
                try:
                    element = browser.ele(selector, timeout=2)
                    if element:
                        element.clear()
                        element.input(code)
                        logger.info(f"[{self.task_type}] SMS code entered into field")
                        time.sleep(1)
                        # Try clicking submit/confirm button
                        submit_selectors = [
                            # Text-based
                            "xpath://button[contains(text(), '确定')]",
                            "xpath://button[contains(text(), '确认')]",
                            "xpath://button[contains(text(), 'Submit')]",
                            "xpath://button[contains(text(), 'Confirm')]",
                            "xpath://button[contains(text(), 'OK')]",
                            # Class-based (XHS red submit buttons)
                            "xpath://button[contains(@class, 'submit')]",
                            "xpath://button[contains(@class, 'confirm')]",
                            "xpath://div[contains(@class, 'submit')]",
                            # Generic: the prominent button in the SMS dialog
                            "xpath://div[contains(@class, 'verification')]//button",
                            "xpath://div[contains(@class, 'verify')]//button",
                        ]
                        for btn_selector in submit_selectors:
                            try:
                                btn = browser.ele(btn_selector, timeout=1)
                                if btn:
                                    btn.click()
                                    logger.info(f"[{self.task_type}] Clicked submit: {btn_selector}")
                                    return True
                            except Exception:
                                continue
                        # Fallback: press Enter
                        logger.info(f"[{self.task_type}] No submit button found, pressing Enter")
                        element.input("\n")
                        return True
                except Exception:
                    continue
            return False
        except Exception as e:
            logger.error(f"[{self.task_type}] Failed to fill SMS code: {e}")
            return False

    def _wait_for_login(self, browser: Any, context_key: str) -> bool:
        """Wait for user to complete manual login.

        Polls the page periodically to check if login is still required.
        Refreshes the login screenshot every 30 seconds so the API always
        serves an up-to-date QR code. If an SMS verification dialog appears,
        polls for user-submitted code via on_poll_user_input callback.

        Args:
            browser: DrissionPage browser instance.
            context_key: Identifier for logging.

        Returns:
            True if login successful, False if timeout reached.
        """
        logger.info(
            f"[{context_key}] Waiting for manual login "
            f"(timeout: {self.login_timeout}s, interval: {self.login_check_interval}s)"
        )
        logger.info(f"[{context_key}] Please login in the browser window...")

        start_time = time.time()
        last_refresh_time = start_time
        sms_detected = False
        refresh_interval = 180  # seconds between page refreshes

        while time.time() - start_time < self.login_timeout:
            time.sleep(self.login_check_interval)
            elapsed = time.time() - start_time
            action_taken = False

            # --- SMS verification (skip first 30s to avoid false positives) ---
            if elapsed >= 30 and self._detect_sms_verification(browser):
                if not sms_detected:
                    sms_detected = True
                    logger.info(f"[{context_key}] SMS verification detected, waiting for code via API")
                    # Send alert with SMS phase so operator knows to reply with code
                    sms_screenshot = self._take_login_screenshot(browser, context_key, phase="sms_verification")
                    self._send_login_alert(context_key, sms_screenshot, phase="sms_verification")
                    action_taken = True

                # Poll for user-submitted code
                if self.on_poll_user_input and self._current_task_id:
                    code = self.on_poll_user_input(self._current_task_id)
                    if code:
                        logger.info(f"[{context_key}] Received SMS code, filling in...")
                        self._fill_sms_code(browser, code)
                        time.sleep(3)
                        sms_detected = False
                        action_taken = True

                if action_taken:
                    self._take_login_screenshot(browser, context_key, phase="sms_verification")
                continue

            # --- Login no longer required → success ---
            if not self._detect_login_required(browser):
                logger.info(
                    f"[{context_key}] Login successful! "
                    f"(waited {elapsed:.1f}s)"
                )
                return True

            # --- Periodic page refresh (every 60s) to detect server-side login ---
            if time.time() - last_refresh_time >= refresh_interval:
                remaining = self.login_timeout - elapsed
                logger.info(
                    f"[{context_key}] Refreshing page to check login status... "
                    f"({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)"
                )
                try:
                    browser.refresh()
                    time.sleep(3)
                except Exception as e:
                    logger.debug(f"[{context_key}] Page refresh error: {e}")
                last_refresh_time = time.time()

                # Re-check after refresh
                if not self._detect_login_required(browser):
                    logger.info(
                        f"[{context_key}] Login successful after page refresh! "
                        f"(waited {elapsed:.1f}s)"
                    )
                    return True

                # Screenshot after refresh (page content may have changed)
                screenshot_path = self._take_login_screenshot(browser, context_key)
                # Re-send alert with updated screenshot
                self._send_login_alert(context_key, screenshot_path)

        logger.warning(f"[{context_key}] Login timeout after {self.login_timeout}s")
        return False

    def _handle_login_if_required(
        self, browser: Any, context_key: str
    ) -> bool:
        """Detect login requirement and handle it (screenshot, alert, wait).

        Orchestrates the full login handling flow:
        1. Detect if login is required
        2. Take screenshot
        3. Send email alert
        4. Wait for manual login (if configured)

        Args:
            browser: DrissionPage browser instance.
            context_key: Identifier for logging.

        Returns:
            True if login not needed or successfully completed.
            False if login required but not resolved (timeout or wait disabled).
        """
        if not self._detect_login_required(browser):
            return True

        logger.info(f"[{context_key}] Login required detected")
        screenshot_path = self._take_login_screenshot(browser, context_key)
        self._send_login_alert(context_key, screenshot_path)

        if self.wait_for_login_enabled:
            if self._wait_for_login(browser, context_key):
                logger.info(f"[{context_key}] Login successful, proceeding")
                if self.on_login_success and self._current_task_id:
                    self.on_login_success(self._current_task_id)
                # Persist cookies for future sessions
                self._save_cookies(browser)
                time.sleep(2)
                return True
            return False

        return False

    # ===================================================================
    # Browser management
    # ===================================================================

    def _connect_to_existing_browser(self) -> Any | None:
        """Connect to an existing Chrome browser via debugging port.

        Returns:
            Browser instance if connected, None otherwise.
        """
        try:
            from DrissionPage import ChromiumPage, ChromiumOptions

            co = ChromiumOptions()
            co.set_local_port(self.chrome_debug_port)
            co.set_argument("--remote-debugging-port", str(self.chrome_debug_port))

            browser = ChromiumPage(addr_or_opts=co)
            current_url = browser.url
            logger.info(
                f"[{self.task_type}] Connected to existing browser at: {current_url}"
            )
            return browser

        except Exception as e:
            logger.error(
                f"[{self.task_type}] Failed to connect to existing browser: {e}"
            )
            return None

    def _prepare_browser(self, browser: Any | None, task_key: str) -> Any:
        """Prepare browser for scraping — connect to existing or use passed browser.

        Args:
            browser: Browser instance passed from worker (may be None).
            task_key: Task key for logging.

        Returns:
            Browser instance to use for scraping.

        Raises:
            ScraperError: If no browser is available.
        """
        if self.use_existing_browser:
            # Health-check cached connection before reuse
            if self._external_browser is not None:
                try:
                    self._external_browser.run_js("1", timeout=3)
                except Exception:
                    logger.warning(
                        f"[{task_key}] Browser connection stale, reconnecting"
                    )
                    self._external_browser = None

            if self._external_browser is None:
                self._external_browser = self._connect_to_existing_browser()

            if self._external_browser is not None:
                logger.info(f"[{task_key}] Using existing browser connection")
                return self._external_browser
            else:
                logger.warning(
                    f"[{task_key}] Failed to connect to existing browser, "
                    "falling back to passed browser"
                )

        if browser is not None:
            return browser

        raise ScraperError(
            f"Browser required for {self.task_type} scraper. "
            "Either pass a browser or enable use_existing_browser.",
            task_key=task_key,
            retryable=False,
        )

    # ===================================================================
    # Cookie persistence
    # ===================================================================

    def _get_cookie_filepath(self) -> str:
        """Get the cookie file path for this scraper type.

        Returns:
            Absolute path to the cookie JSON file.
        """
        os.makedirs(self.cookies_dir, exist_ok=True)
        return os.path.join(self.cookies_dir, f"{self.task_type}_cookies.json")

    def _save_cookies(self, browser: Any) -> bool:
        """Save browser cookies to a JSON file for session persistence.

        Args:
            browser: DrissionPage browser instance.

        Returns:
            True if cookies were saved successfully.
        """
        try:
            cookies = browser.cookies(all_info=True)
            if not cookies:
                logger.debug(f"[{self.task_type}] No cookies to save")
                return False

            cookie_data = {
                "saved_at": datetime.now().isoformat(),
                "task_type": self.task_type,
                "cookies": cookies,
            }

            filepath = self._get_cookie_filepath()
            with open(filepath, "w", encoding="utf-8") as f:
                _json.dump(cookie_data, f, ensure_ascii=False, default=str)

            logger.info(
                f"[{self.task_type}] Saved {len(cookies)} cookies to {filepath}"
            )
            return True

        except Exception as e:
            logger.warning(f"[{self.task_type}] Failed to save cookies: {e}")
            return False

    def _restore_cookies(self, browser: Any) -> bool:
        """Restore cookies from a JSON file to the browser.

        Checks cookie age against cookie_max_age_hours before restoring.

        Args:
            browser: DrissionPage browser instance.

        Returns:
            True if cookies were restored successfully.
        """
        filepath = self._get_cookie_filepath()
        if not os.path.exists(filepath):
            logger.debug(f"[{self.task_type}] No cookie file found at {filepath}")
            return False

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                cookie_data = _json.load(f)

            # Check cookie age
            saved_at = datetime.fromisoformat(cookie_data.get("saved_at", ""))
            age_hours = (datetime.now() - saved_at).total_seconds() / 3600
            if age_hours > self.cookie_max_age_hours:
                logger.info(
                    f"[{self.task_type}] Cookies expired "
                    f"({age_hours:.1f}h > {self.cookie_max_age_hours}h), discarding"
                )
                os.remove(filepath)
                return False

            cookies = cookie_data.get("cookies", [])
            if not cookies:
                return False

            # Restore cookies to browser
            for cookie in cookies:
                try:
                    if isinstance(cookie, dict):
                        browser.set.cookies(cookie)
                    else:
                        browser.set.cookies(cookie)
                except Exception:
                    continue

            logger.info(
                f"[{self.task_type}] Restored {len(cookies)} cookies "
                f"(age: {age_hours:.1f}h)"
            )
            return True

        except Exception as e:
            logger.warning(f"[{self.task_type}] Failed to restore cookies: {e}")
            return False

    def _delete_cookies(self) -> None:
        """Delete the saved cookie file."""
        filepath = self._get_cookie_filepath()
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"[{self.task_type}] Deleted cookie file: {filepath}")

    # ===================================================================
    # Debug file saving
    # ===================================================================

    def _init_run_screenshots_dir(self, context_key: str) -> None:
        """Create a per-run subdirectory for screenshots.

        Called once at the start of each scrape task. All screenshots for this
        run are saved sequentially (001_xxx.png, 002_xxx.png, ...) in the same
        subdirectory for easy review.

        Args:
            context_key: Identifier for the current operation (e.g., account_id).
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_key = re.sub(r'[^\w\-]', '_', context_key)
        self._run_screenshots_dir = os.path.join(
            self.screenshots_dir, f"{safe_key}_{timestamp}"
        )
        os.makedirs(self._run_screenshots_dir, exist_ok=True)
        self._screenshot_counter = 0
        logger.debug(
            f"[{context_key}] Screenshots dir: {self._run_screenshots_dir}"
        )

    def _save_page_screenshot(
        self, browser: Any, context_key: str, page_name: str
    ) -> str | None:
        """Save a screenshot of the current page for debugging.

        Screenshots are saved sequentially (001_xxx.png, 002_xxx.png, ...)
        inside a per-run subdirectory.

        Args:
            browser: DrissionPage browser instance.
            context_key: Identifier for the current operation.
            page_name: Descriptive name (e.g., "search", "profile").

        Returns:
            Path to saved screenshot or None if failed.
        """
        try:
            self._screenshot_counter += 1
            seq = f"{self._screenshot_counter:03d}"

            if self._run_screenshots_dir:
                save_dir = self._run_screenshots_dir
            else:
                save_dir = self.screenshots_dir

            os.makedirs(save_dir, exist_ok=True)
            filename = f"{seq}_{page_name}.png"
            filepath = os.path.join(save_dir, filename)

            if hasattr(browser, "get_screenshot"):
                browser.get_screenshot(path=filepath)
            elif hasattr(browser, "save_screenshot"):
                browser.save_screenshot(filepath)

            logger.info(f"[{context_key}] Screenshot saved: {filepath}")

            # Upload to S3 if enabled
            final_path = self._handle_upload(filepath)

            # Store S3 URL (or local path) in DB for API access
            if self.on_page_screenshot and self._current_task_id:
                try:
                    self.on_page_screenshot(self._current_task_id, final_path)
                except Exception as e:
                    logger.debug(f"[{context_key}] Failed to store screenshot path: {e}")

            return final_path

        except Exception as e:
            logger.warning(f"[{context_key}] Failed to save screenshot: {e}")
            return None

    def _save_page_html(
        self, browser: Any, context_key: str, page_name: str
    ) -> str | None:
        """Save HTML of the current page for debugging.

        Args:
            browser: DrissionPage browser instance.
            context_key: Identifier for the current operation.
            page_name: Descriptive name (e.g., "search", "profile").

        Returns:
            Path to saved HTML file or None if failed.
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_key = re.sub(r'[^\w\-]', '_', context_key)
            filename = f"{safe_key}_{page_name}_{timestamp}.html"
            filepath = os.path.join(self.screenshots_dir, filename)

            html_content = browser.html
            if html_content:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(html_content)
                logger.info(f"[{context_key}] HTML saved: {filepath}")

                # Upload to S3 if enabled
                final_path = self._handle_upload(filepath)
                return final_path

            return None

        except Exception as e:
            logger.warning(f"[{context_key}] Failed to save HTML: {e}")
            return None

    # ===================================================================
    # Cookie consent & Cloudflare
    # ===================================================================

    def _dismiss_cookie_consent(
        self, browser: Any, context_key: str = ""
    ) -> None:
        """Dismiss cookie consent dialogs commonly found on websites.

        Tries multiple selector strategies to find and click dismiss/reject buttons.
        Safe to call even if no dialog is present.

        Args:
            browser: Browser instance.
            context_key: Identifier for logging.
        """
        try:
            consent_selectors = [
                "text:Disagree and close",
                "text:Reject all",
                "text:Agree and close",
                "text:Accept all",
                "text:Agree",
                "button:contains(Disagree)",
                "button:contains(Reject)",
            ]

            for selector in consent_selectors:
                try:
                    btn = browser.ele(selector, timeout=2)
                    if btn:
                        try:
                            if btn.states.is_displayed:
                                btn.click()
                                logger.info(
                                    f"[{context_key}] Dismissed cookie consent "
                                    f"via '{selector}'"
                                )
                                time.sleep(2)
                                return
                        except Exception:
                            btn.click()
                            logger.info(
                                f"[{context_key}] Dismissed cookie consent "
                                f"via '{selector}'"
                            )
                            time.sleep(2)
                            return
                except Exception:
                    continue

            # Try FC-specific consent button (Funding Choices)
            try:
                btn = browser.ele("@class=fc-cta-consent", timeout=2)
                if btn:
                    btn.click()
                    logger.info(
                        f"[{context_key}] Dismissed cookie consent via fc-cta-consent"
                    )
                    time.sleep(2)
                    return
            except Exception:
                pass

            logger.debug(
                f"[{context_key}] No cookie consent dialog found or already dismissed"
            )

        except Exception as e:
            logger.debug(f"[{context_key}] Error handling cookie consent: {e}")

    def handle_cloudflare(
        self,
        browser: Any,
        max_wait: int = 120,
        screenshot_dir: str = "data/cloudflare_screenshots",
    ) -> bool:
        """Handle Cloudflare challenge if detected.

        Args:
            browser: Browser instance.
            max_wait: Maximum seconds to wait for challenge resolution.
            screenshot_dir: Directory to save debug screenshots.

        Returns:
            True if challenge was resolved or not present.
        """
        if not self.cloudflare_protected:
            return True

        html = browser.html.lower()
        title = (browser.title or "").lower()

        cf_title_indicators = [
            "just a moment",
            "attention required",
            "checking your browser",
        ]
        cf_body_indicators = [
            "checking if the site connection is secure",
            "enable javascript and cookies to continue",
            "ray id:",
        ]

        if not any(ind in title for ind in cf_title_indicators):
            if not any(ind in html for ind in cf_body_indicators):
                return True

        logger.info(
            f"[{self.task_type}] Cloudflare challenge detected, "
            f"waiting up to {max_wait}s..."
        )

        self._save_cloudflare_screenshot(browser, screenshot_dir, "initial")

        start_time = time.time()
        check_count = 0

        while time.time() - start_time < max_wait:
            time.sleep(5)
            check_count += 1

            try:
                html = browser.html.lower()
                title = (browser.title or "").lower()
            except Exception:
                continue

            title_clear = not any(ind in title for ind in cf_title_indicators)
            body_clear = not any(ind in html for ind in cf_body_indicators)

            if title_clear and body_clear:
                elapsed = time.time() - start_time
                logger.info(
                    f"[{self.task_type}] Cloudflare challenge resolved "
                    f"after {elapsed:.1f}s"
                )
                return True

            if check_count % 6 == 0:
                elapsed = int(time.time() - start_time)
                self._save_cloudflare_screenshot(
                    browser, screenshot_dir, f"check_{elapsed}s"
                )

        self._save_cloudflare_screenshot(browser, screenshot_dir, "timeout")
        logger.warning(
            f"[{self.task_type}] Cloudflare challenge timeout after {max_wait}s"
        )
        return False

    def _save_cloudflare_screenshot(
        self, browser: Any, screenshot_dir: str, suffix: str
    ) -> None:
        """Save a screenshot for Cloudflare debugging."""
        try:
            os.makedirs(screenshot_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = (
                f"{screenshot_dir}/cf_{self.task_type}_{timestamp}_{suffix}.png"
            )

            if hasattr(browser, "get_screenshot"):
                browser.get_screenshot(path=filename)
            elif hasattr(browser, "save_screenshot"):
                browser.save_screenshot(filename)
            else:
                browser.get_screenshot(path=filename, full_page=True)

            logger.info(f"[{self.task_type}] Saved Cloudflare screenshot: {filename}")
        except Exception as e:
            logger.warning(f"[{self.task_type}] Failed to save screenshot: {e}")

    # ===================================================================
    # Scroll-to-load pagination
    # ===================================================================

    def _scroll_to_load_more(
        self, browser: Any, elements: list[Any], context_key: str
    ) -> None:
        """Scroll to load more content using multiple strategies.

        Args:
            browser: DrissionPage browser instance.
            elements: Current list of visible elements.
            context_key: Identifier for logging.

        Raises:
            Exception: Re-raises connection errors for reconnection handling.
        """
        try:
            # Strategy 1: Scroll last element into view
            if elements:
                last_element = elements[-1]
                try:
                    browser.run_js(
                        "arguments[0].scrollIntoView("
                        "{behavior: 'smooth', block: 'end'})",
                        last_element,
                    )
                    time.sleep(1)
                except Exception:
                    pass

            # Strategy 2: Scroll to page bottom
            browser.run_js("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

            # Strategy 3: Additional scroll
            browser.run_js("window.scrollBy(0, 500)")
            time.sleep(1)

        except Exception as e:
            error_msg = str(e).lower()
            if "disconnected" in error_msg or "connection" in error_msg:
                raise
            logger.debug(f"[{context_key}] Scroll error (non-fatal): {e}")

    # ===================================================================
    # Overlay and modal handling
    # ===================================================================

    def _dismiss_overlays(self, browser: Any) -> None:
        """Dismiss any overlays or popups that might block clicks.

        Uses OVERLAY_CLOSE_SELECTORS defined as a class attribute,
        plus an ESC key press as fallback.

        Args:
            browser: DrissionPage browser instance.
        """
        try:
            for selector in self.OVERLAY_CLOSE_SELECTORS:
                try:
                    close_btn = browser.ele(selector, timeout=0.3)
                    if close_btn:
                        close_btn.click()
                        time.sleep(0.2)
                        logger.debug(f"Dismissed overlay with: {selector}")
                except Exception:
                    continue

            # Press ESC as fallback
            try:
                browser.run_js(
                    "document.dispatchEvent("
                    "new KeyboardEvent('keydown', {'key': 'Escape'}))"
                )
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Error dismissing overlays: {e}")

    def _close_modal(self, browser: Any) -> None:
        """Close a modal dialog or navigate back.

        Tries multiple strategies in order:
        1. ESC key press
        2. Click close button (using MODAL_CLOSE_SELECTORS)
        3. Click outside the modal
        4. Browser history back

        Each step short-circuits if the modal is already gone, so a successful
        ESC never falls through to history.back() — that fallback is reserved
        for the rare case where the detail view is its own URL rather than an
        in-page modal, and firing it unnecessarily can navigate away from the
        list page we want to stay on.

        Args:
            browser: DrissionPage browser instance.
        """
        try:
            # Method 1: ESC key
            try:
                browser.run_js(
                    "document.dispatchEvent("
                    "new KeyboardEvent('keydown', {'key': 'Escape'}))"
                )
                time.sleep(0.5)
            except Exception:
                pass
            if not self._is_modal_open(browser):
                return

            # Method 2: Close button
            for selector in self.MODAL_CLOSE_SELECTORS:
                try:
                    close_btn = browser.ele(selector, timeout=1)
                    if close_btn:
                        close_btn.click()
                        time.sleep(0.5)
                        if not self._is_modal_open(browser):
                            return
                except Exception:
                    continue

            # Method 3: Click outside
            try:
                browser.run_js("document.body.click()")
                time.sleep(0.3)
            except Exception:
                pass
            if not self._is_modal_open(browser):
                return

            # Method 4: History back
            try:
                browser.run_js("window.history.back()")
                time.sleep(1)
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Error closing modal: {e}")

    def _is_modal_open(self, browser: Any) -> bool:
        """Best-effort check for whether a modal is still visible.

        Uses MODAL_CLOSE_SELECTORS as a proxy: if any configured close button
        is still present in the DOM, we assume the modal is open. Absence of
        all selectors means the modal is gone (or was never there), and the
        caller can skip further dismissal strategies.
        """
        for selector in self.MODAL_CLOSE_SELECTORS:
            try:
                if browser.ele(selector, timeout=0.3):
                    return True
            except Exception:
                continue
        return False

    # ===================================================================
    # S3 upload
    # ===================================================================

    def _upload_to_s3(self, local_path: str) -> str | None:
        """Upload a file to S3.

        Args:
            local_path: Local file path.

        Returns:
            Full S3 URL if successful, None otherwise.
        """
        if not self.s3_client or not os.path.exists(local_path):
            return None

        try:
            from botocore.exceptions import ClientError

            # Preserve subdirectory structure relative to screenshots_dir
            try:
                rel_path = os.path.relpath(local_path, self.screenshots_dir)
            except ValueError:
                rel_path = os.path.basename(local_path)
            s3_key = f"{self.s3_prefix}/{rel_path}" if self.s3_prefix else rel_path
            s3_key = os.path.normpath(s3_key).replace("\\", "/")

            # Determine content type from extension
            content_type = "application/octet-stream"
            ext = local_path.lower().rsplit(".", 1)[-1] if "." in local_path else ""
            content_type_map = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
                "gif": "image/gif",
                "avif": "image/avif",
                "html": "text/html",
            }
            content_type = content_type_map.get(ext, content_type)

            self.s3_client.upload_file(
                local_path,
                self.s3_bucket,
                s3_key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": "public, max-age=86400",
                },
            )

            logger.info(f"[{self.task_type}] Uploaded to S3: s3://{self.s3_bucket}/{s3_key}")

            if self.delete_local_after_upload:
                try:
                    os.remove(local_path)
                except OSError as e:
                    logger.warning(f"Failed to delete local file: {e}")

            return s3_key

        except Exception as e:
            logger.error(f"[{self.task_type}] S3 upload failed: {e}")
            return None

    def _handle_upload(self, local_path: str) -> str:
        """Handle S3 upload if enabled, otherwise return relative path.

        Args:
            local_path: Local file path.

        Returns:
            S3 key if uploaded, relative path otherwise.
        """
        if self.s3_enabled and self.s3_client and self.s3_bucket:
            s3_key = self._upload_to_s3(local_path)
            if s3_key:
                return s3_key

        return self._get_relative_path(local_path)

    def _get_relative_path(self, local_path: str) -> str:
        """Convert local path to relative path for storage.

        Args:
            local_path: Local filesystem path.

        Returns:
            Relative path suitable for database storage.
        """
        try:
            rel_path = os.path.relpath(local_path, self.screenshots_dir)
        except ValueError:
            rel_path = os.path.basename(local_path)
        result = f"{self.s3_prefix}/{rel_path}" if self.s3_prefix else rel_path
        return os.path.normpath(result).replace("\\", "/")

    # ===================================================================
    # Utility methods
    # ===================================================================

    def _find_element(
        self, browser: Any, selectors: list[str], timeout: float = 1
    ) -> Any | None:
        """Find an element by trying multiple selectors in priority order.

        Args:
            browser: DrissionPage browser instance.
            selectors: List of CSS/XPath selectors to try.
            timeout: Timeout per selector in seconds.

        Returns:
            First matching element or None.
        """
        for selector in selectors:
            try:
                elem = browser.ele(selector, timeout=timeout)
                if elem:
                    return elem
            except Exception:
                continue
        return None
