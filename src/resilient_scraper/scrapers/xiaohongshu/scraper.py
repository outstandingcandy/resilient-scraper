"""
Xiaohongshu (Little Red Book) scraper implementation.

Scrapes user profiles and notes from xiaohongshu.com with support for:
- Account search and profile navigation
- Note extraction with images, content, and metadata
- Login detection with screenshot and email alert
- S3 upload integration
- PostgreSQL database persistence
"""

import logging
import os
import re
import time
from typing import Any

import requests
from sqlalchemy import create_engine

from resilient_scraper import ResilientScraper
from resilient_scraper.errors import (
    BrowserDisconnectedError,
    NoDataFoundError,
    ScraperError,
)
from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.xiaohongshu.db import XiaohongshuDB
from resilient_scraper.scrapers.xiaohongshu.parser import XiaohongshuParser
from resilient_scraper.scrapers.xiaohongshu.models import (
    XiaohongshuAuthor,
    XiaohongshuComment,
    XiaohongshuFollowing,
    XiaohongshuFollowingResult,
    XiaohongshuNote,
    XiaohongshuResult,
    XiaohongshuSearchAuthorResult,
)

logger = logging.getLogger("scraper.xiaohongshu")


class XiaohongshuScraper(ResilientScraper[XiaohongshuResult]):
    """Scraper for Xiaohongshu (Little Red Book) user profiles and notes.

    Configuration options (in scraper config):
        max_notes: Maximum notes to scrape per account (default: 50)
        images_dir: Local directory for images (default: "data/xiaohongshu_images")
        screenshots_dir: Directory for login screenshots (default: "data/xiaohongshu_screenshots")
        s3_upload: Whether to upload images to S3 (default: False)
        s3_bucket: S3 bucket name (required if s3_upload is True)
        s3_prefix: S3 key prefix (default: "data/xiaohongshu_images")
        login_alert_email: Email to send login alerts to
        smtp_server: SMTP server for alerts (default: "smtp.qq.com")
        smtp_port: SMTP port (default: 465)
        smtp_sender: SMTP sender email
        smtp_password: SMTP password
        wait_for_login: Wait for manual login when required (default: True)
        login_check_interval: Seconds between login checks (default: 5)
        login_timeout: Max seconds to wait for login (default: 300)

    Task payload options:
        max_notes: Override max_notes for this task
    """

    task_type = "xiaohongshu"
    default_delay = (10.0, 20.0)
    requires_browser = True
    cloudflare_protected = False
    task_timeout = 86400  # 24 hours — scrapes many notes with comments and images

    platform_display_name = "Xiaohongshu (Little Red Book)"

    # Login indicators to detect login requirement
    # Note: Keep indicators specific to avoid false positives
    LOGIN_INDICATORS = [
        # Chinese indicators (most reliable)
        "手机号登录",
        "登录后查看搜索结果",
        "输入手机号",
        "获取验证码",
        "请先登录",
        "登录小红书",
        "密码登录",
        "短信验证码登录",
        # English indicators (only very specific ones)
        "Log in to continue",
        "Sign in to continue",
    ]

    LOGIN_SELECTORS = [
        "xpath://input[@placeholder='手机号']",
        "xpath://input[@placeholder='输入手机号']",
        "xpath://div[contains(@class, 'login-modal')]",
        "xpath://div[contains(@class, 'login-container')]",
        "xpath://div[contains(@class, 'qrcode-login')]",
        "xpath://form[contains(@class, 'login')]",
    ]

    # Selectors that only appear when the user is logged in.
    # If any matches a visible element, detection short-circuits to "not login required".
    LOGGED_IN_SELECTORS = [
        "xpath://*[contains(@class, 'user-avatar')]",
        "xpath://*[contains(@class, 'avatar-wrapper')]",
        "xpath://a[contains(@class, 'user')]//img",
        "xpath://*[contains(@class, 'side-bar-component')]//*[contains(@class, 'user')]",
    ]

    # URL patterns that indicate login/captcha is required
    LOGIN_URL_PATTERNS = ["captcha", "website-login", "login/captcha"]

    # Selectors to click to trigger login popup (e.g., "我" button in sidebar)
    LOGIN_TRIGGER_SELECTORS = [
        "xpath://a[contains(text(),'我')]",
        "xpath://span[contains(text(),'我')]",
        "xpath://*[contains(@class,'side')]//a[contains(text(),'我')]",
        "xpath://li[contains(text(),'我')]",
    ]

    OVERLAY_CLOSE_SELECTORS = [
        "xpath://div[contains(@class, 'login')]//button[contains(@class, 'close')]",
        "xpath://div[contains(@class, 'modal')]//span[contains(@class, 'close')]",
        "xpath://div[contains(@class, 'ad')]//button[contains(text(), '关闭')]",
        "xpath://div[contains(@class, 'ad')]//span[contains(@class, 'close')]",
        "xpath://div[contains(@class, 'overlay')]//button",
        "xpath://div[contains(@class, 'mask')]//div[contains(@class, 'close')]",
    ]

    MODAL_CLOSE_SELECTORS = [
        "xpath://div[contains(@class, 'close')]",
        "xpath://button[contains(@class, 'close')]",
        "xpath://span[contains(@class, 'close')]",
        "xpath://*[contains(@class, 'note-detail')]//div[contains(@class, 'close')]",
    ]

    # SMS verification indicators — specific to the post-QR-scan SMS dialog.
    # The dialog shows English text: "SMS Verification" title and
    # "Verification code has been sent to +86 ...".
    # Avoid generic "短信验证" which matches "短信验证码登录" on the QR page.
    SMS_VERIFICATION_INDICATORS = [
        "SMS Verification",
        "Verification code has been sent",
        "验证码已发送",
        "Didn't receive code",
    ]

    SMS_VERIFICATION_SELECTORS = [
        "xpath://input[@placeholder='Please enter verification code']",
        "xpath://input[@placeholder='请输入验证码']",
        "xpath://input[@placeholder='输入验证码']",
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the Xiaohongshu scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)

        # XHS-specific configurations
        self.max_notes = self.config.get("max_notes", 0)
        self.images_dir = self.config.get("images_dir", "data/xiaohongshu_images")

        # Skip video notes - only download image notes
        self.skip_video_notes = self.config.get("skip_video_notes", True)

        # Comment extraction configuration
        self.extract_comments = self.config.get("extract_comments", True)
        self.max_comments_per_note = self.config.get("max_comments_per_note", 100)
        self.max_replies_per_comment = self.config.get("max_replies_per_comment", 50)
        self.expand_replies = self.config.get("expand_replies", True)

        # Skip existing notes - load from database to avoid re-scraping
        self.skip_existing_notes = self.config.get("skip_existing_notes", True)
        self.skip_existing_days = int(self.config.get("skip_existing_days", 0))

    _login_url_logged: bool = False

    SERVER_ERROR_INDICATORS = ["未连接到服务器", "点击刷新"]

    def _detect_server_error(self, browser: Any) -> bool:
        """Check if the current page is a server error page."""
        try:
            page_text = browser.html or ""
            return any(indicator in page_text for indicator in self.SERVER_ERROR_INDICATORS)
        except Exception:
            return False

    def _retry_on_server_error(
        self, browser: Any, url: str, display_name: str, context: str, max_retries: int = 3
    ) -> bool:
        """Detect server error page and retry with escalating strategies.

        Args:
            browser: DrissionPage browser instance.
            url: URL to retry navigating to.
            display_name: Display name for logging.
            context: Page context for screenshot naming (e.g., "homepage", "profile").
            max_retries: Maximum number of retries.

        Returns:
            True if page recovered, False if all retries exhausted.
        """
        for attempt in range(max_retries):
            if not self._detect_server_error(browser):
                return True

            logger.warning(
                f"[{display_name}] Server error on {context} page "
                f"(attempt {attempt + 1}/{max_retries})"
            )
            self._save_page_screenshot(browser, display_name, f"{context}_error_{attempt + 1}")

            if attempt == 0:
                # First retry: click the refresh button
                clicked = False
                for selector in (
                    "xpath://button[contains(text(),'点击刷新')]",
                    "xpath://a[contains(text(),'点击刷新')]",
                    "xpath://span[contains(text(),'点击刷新')]",
                    "xpath://*[contains(text(),'点击刷新')]",
                ):
                    try:
                        btn = browser.ele(selector, timeout=2)
                        if btn:
                            btn.click()
                            clicked = True
                            logger.info(f"[{display_name}] Clicked refresh button")
                            break
                    except Exception:
                        continue
                if not clicked:
                    browser.get(url)
                time.sleep(5)
            else:
                # Subsequent retries: go back to homepage to reset state
                logger.info(f"[{display_name}] Returning to homepage to reset browsing state")
                browser.get("https://www.xiaohongshu.com/explore")
                time.sleep(5 + attempt * 3)
                logger.info(f"[{display_name}] Re-navigating to: {url}")
                browser.get(url)
                time.sleep(5 + attempt * 2)

        return not self._detect_server_error(browser)

    def _trigger_login_popup(self, browser: Any, display_name: str) -> None:
        """Click sidebar elements (e.g., "我") to trigger the login QR code popup."""
        for selector in self.LOGIN_TRIGGER_SELECTORS:
            try:
                btn = browser.ele(selector, timeout=2)
                if btn:
                    btn.click()
                    logger.info(f"[{display_name}] Clicked login trigger: {selector}")
                    time.sleep(3)
                    return
            except Exception:
                continue
        # Fallback: if no trigger found, try navigating to explore page first
        logger.warning(f"[{display_name}] No login trigger found on current page, navigating to explore")
        browser.get("https://www.xiaohongshu.com/explore")
        time.sleep(3)
        for selector in self.LOGIN_TRIGGER_SELECTORS:
            try:
                btn = browser.ele(selector, timeout=2)
                if btn:
                    btn.click()
                    logger.info(f"[{display_name}] Clicked login trigger after navigate: {selector}")
                    time.sleep(3)
                    return
            except Exception:
                continue
        logger.warning(f"[{display_name}] Failed to trigger login popup via click")

    def _detect_login_required(self, browser: Any) -> bool:
        """Check if login is required.

        Avoids false positives on the XHS SPA by:
          1. Short-circuiting on a positive "logged-in" signal (avatar/user menu).
          2. Matching LOGIN_INDICATORS only against visible body text, not the
             full HTML source (which contains pre-rendered login modals and
             i18n strings even when the user is already logged in).
          3. Requiring a visible LOGIN_SELECTORS element to corroborate a text
             match before reporting login required.
        """
        # 1. URL-based captcha detection
        try:
            url = browser.url or ""
            for pattern in self.LOGIN_URL_PATTERNS:
                if pattern in url:
                    if not self._login_url_logged:
                        logger.warning(
                            f"[{self.task_type}] Login/captcha URL detected: {url}"
                        )
                        self._login_url_logged = True
                    return True
        except Exception:
            pass
        self._login_url_logged = False

        # 2. Positive signal: if a logged-in element is visible, short-circuit.
        for selector in self.LOGGED_IN_SELECTORS:
            try:
                el = browser.ele(selector, timeout=0.5)
                if el:
                    logger.debug(
                        f"[{self.task_type}] Logged-in signal: {selector}"
                    )
                    return False
            except Exception:
                continue

        # 3. Text check on visible body text only (not HTML source).
        page_text = ""
        try:
            body = browser.ele("tag:body", timeout=1)
            if body:
                page_text = (body.text or "")
        except Exception:
            page_text = ""

        text_match = None
        if page_text:
            lowered = page_text.lower()
            for indicator in self.LOGIN_INDICATORS:
                if indicator.lower() in lowered:
                    text_match = indicator
                    break

        if not text_match:
            return False

        # 4. Corroborate with a visible login element before reporting.
        for selector in self.LOGIN_SELECTORS:
            try:
                el = browser.ele(selector, timeout=1)
                if el:
                    logger.debug(
                        f"[{self.task_type}] Login required — text '{text_match}' "
                        f"and selector '{selector}' both present"
                    )
                    return True
            except Exception:
                continue

        logger.debug(
            f"[{self.task_type}] Login text '{text_match}' matched but no login "
            f"element visible; treating as not-required"
        )
        return False

    def setup(self) -> None:
        """Setup scraper resources and create database tables."""
        super().setup()

        # Ensure XHS-specific directories exist
        os.makedirs(self.images_dir, exist_ok=True)

        # Override parent's DB engine with connect_timeout for XHS
        if self.database_url:
            try:
                connect_args = {"connect_timeout": 5}  # 5 second timeout
                self.db_engine = create_engine(
                    self.database_url,
                    echo=False,
                    pool_pre_ping=True,
                    connect_args=connect_args,
                )
                self.db = XiaohongshuDB(self.db_engine, self.s3_bucket, self.s3_prefix)
                self.parser = XiaohongshuParser()
                self.db.ensure_tables_exist()
                logger.info("Database connection established")
            except Exception as e:
                logger.error(f"Failed to initialize database: {e}")
                self.db_engine = None  # Disable DB if connection fails

        # Ensure db/parser are always available even without database
        if not hasattr(self, 'db'):
            self.db = XiaohongshuDB(None, self.s3_bucket, self.s3_prefix)
        if not hasattr(self, 'parser'):
            self.parser = XiaohongshuParser()

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that the task can be processed.

        Args:
            task: The task to validate.

        Returns:
            True if valid, False otherwise.
        """
        if not task.task_key:
            return False

        account_id = task.task_key.strip()

        # Account ID should be non-empty and reasonable length
        if len(account_id) < 1 or len(account_id) > 100:
            return False

        # Check for invalid values
        invalid_values = {"", "UNKNOWN", "N/A", "NA", "NONE", "NULL", "TEST"}
        if account_id.upper() in invalid_values:
            return False

        return True

    def build_url(self, task: ScraperTask) -> str:
        """Build the Xiaohongshu direct profile URL for an account.

        Args:
            task: The task with account ID (user_id) as task_key.

        Returns:
            Xiaohongshu profile URL.
        """
        account_id = task.task_key.strip()
        # Use direct profile URL instead of search
        return f"https://www.xiaohongshu.com/user/profile/{account_id}"

    def _handle_login_flow(
        self,
        browser: Any,
        account_id: str,
        display_name: str,
        *,
        trigger_popup: bool = True,
    ) -> str | None:
        """Handle login flow: optionally trigger popup → screenshot → alert → wait.

        Call this when login is determined to be needed.

        Args:
            browser: Browser instance.
            account_id: Account being scraped.
            display_name: Display name for logging.
            trigger_popup: Whether to click login trigger first.

        Returns:
            None if login succeeded, screenshot_path if failed/timed out.
        """
        if trigger_popup:
            self._trigger_login_popup(browser, display_name)
            self._save_page_screenshot(browser, account_id, "login_trigger_clicked")

        screenshot_path = self._take_login_screenshot(browser, account_id)
        self._send_login_alert(account_id, screenshot_path)

        if not self.wait_for_login_enabled:
            return screenshot_path

        if not self._wait_for_login(browser, account_id):
            return screenshot_path

        logger.info(f"[{display_name}] Login successful")
        if self.on_login_success and self._current_task_id:
            self.on_login_success(self._current_task_id)
        self._save_cookies(browser)
        self._save_page_screenshot(browser, account_id, "after_login")
        return None

    def _login_failed_result(
        self,
        task: ScraperTask,
        account_id: str,
        screenshot_path: str | None = None,
        error: str = "Login required",
    ) -> XiaohongshuResult:
        """Build a login-failure result."""
        return XiaohongshuResult(
            success=False,
            task_key=task.task_key,
            task_type=self.task_type,
            account_id=account_id,
            login_required=True,
            login_screenshot_path=screenshot_path,
            error=error,
        )

    def scrape(
        self, task: ScraperTask, browser: Any | None = None
    ) -> XiaohongshuResult:
        """Scrape notes from a Xiaohongshu account.

        Args:
            task: Task with account ID as task_key.
            browser: DrissionPage browser instance (optional if use_existing_browser=True).

        Returns:
            XiaohongshuResult with scraped notes and metadata.

        Raises:
            ScraperError: If scraping fails.
        """
        # Prepare browser - use existing browser connection or passed browser
        browser = self._prepare_browser(browser, task.task_key)

        account_id = task.task_key.strip()
        # Use nickname from payload for logging if available, fallback to user_id
        display_name = task.payload.get("nickname") or account_id
        max_notes = task.payload.get("max_notes", self.max_notes) or float("inf")

        logger.info(f"[{display_name}] Starting scrape (max_notes={max_notes})")

        # Initialize per-run screenshots directory
        self._init_run_screenshots_dir(account_id)

        # Step 0: Restore cookies from previous session (avoids re-login)
        self._restore_cookies(browser)

        # Step 1: Navigate to homepage
        homepage_url = "https://www.xiaohongshu.com/explore"
        logger.info(f"[{display_name}] Opening Xiaohongshu homepage")
        browser.get(homepage_url)
        time.sleep(3)

        # Step 1b: Check for server error on homepage
        if not self._retry_on_server_error(browser, homepage_url, display_name, "homepage"):
            logger.warning(
                f"[{display_name}] Server error persists on homepage, "
                "triggering login flow"
            )
            failed_ss = self._handle_login_flow(browser, account_id, display_name)
            if failed_ss is not None:
                return self._login_failed_result(
                    task, account_id, failed_ss,
                    "Server error on homepage - login required",
                )
            browser.get(homepage_url)
            time.sleep(3)

        self._save_page_screenshot(browser, account_id, "homepage")

        # Step 2: Check login status FIRST
        if self._detect_login_required(browser):
            logger.info(f"[{display_name}] Login required, waiting for login")
            failed_ss = self._handle_login_flow(
                browser, account_id, display_name, trigger_popup=False,
            )
            if failed_ss is not None:
                return self._login_failed_result(
                    task, account_id, failed_ss, "Login required to continue",
                )
            time.sleep(2)

        # Step 3: Search user ID and navigate to profile via click
        profile_url = self.build_url(task)
        logger.info(f"[{display_name}] Searching for user: {account_id}")
        searched = self._perform_search(browser, account_id)

        if not searched:
            logger.warning(f"[{display_name}] Search box not available")
            self._save_page_screenshot(browser, account_id, "search_failed")
            # If search fails, check if it's a login issue
            if self._detect_server_error(browser) or self._detect_login_required(browser):
                failed_ss = self._handle_login_flow(browser, account_id, display_name)
                if failed_ss is not None:
                    return self._login_failed_result(
                        task, account_id, failed_ss, "Login timeout",
                    )
                searched = self._perform_search(browser, account_id)
                self._save_page_screenshot(browser, account_id, "after_search_retry")

        # Step 4: Click through search results to user profile
        if searched:
            result_url, browser = self._navigate_to_user_profile(browser, account_id)
            logger.info(f"[{display_name}] Navigation result: {result_url}, url: {browser.url}")

            if result_url == "LOGIN_REQUIRED":
                logger.warning(f"[{display_name}] Login required in search results")
                failed_ss = self._handle_login_flow(browser, account_id, display_name)
                if failed_ss is not None:
                    return self._login_failed_result(
                        task, account_id, failed_ss,
                        "Login timeout - in search results",
                    )
                self._perform_search(browser, account_id)
                self._save_page_screenshot(browser, account_id, "after_search_retry")
                result_url, browser = self._navigate_to_user_profile(browser, account_id)
                self._save_page_screenshot(browser, account_id, "after_click_user_retry")

            if result_url == "USER_ID_MISMATCH" or result_url is None:
                logger.warning(f"[{display_name}] Could not navigate to profile via search")
                self._save_page_screenshot(browser, account_id, "search_nav_failed")

        # Step 5: Check if we landed on profile page
        if "/user/profile/" not in (browser.url or ""):
            # Not on profile — check what went wrong
            self._save_page_screenshot(browser, account_id, "not_on_profile")

            if self._detect_server_error(browser) or self._detect_login_required(browser):
                logger.warning(f"[{display_name}] Login/server error, triggering login")
                failed_ss = self._handle_login_flow(browser, account_id, display_name)
                if failed_ss is not None:
                    return self._login_failed_result(
                        task, account_id, failed_ss, "Login timeout",
                    )
                self._perform_search(browser, account_id)
                self._save_page_screenshot(browser, account_id, "after_search_retry")
                result_url, browser = self._navigate_to_user_profile(browser, account_id)
                self._save_page_screenshot(browser, account_id, "after_click_user_retry")

            # Final check
            if "/user/profile/" not in (browser.url or ""):
                logger.warning(f"[{display_name}] Still not on profile page: {browser.url}")
                self._save_page_screenshot(browser, account_id, "final_not_on_profile")
                raise NoDataFoundError(task_key=task.task_key)

        # Step 6: Profile page loaded successfully
        logger.info(f"[{display_name}] On profile page: {browser.url}")
        self._save_page_screenshot(browser, account_id, "profile")

        # Step 7: Extract author information (using new browser/tab if opened)
        author = self.parser.extract_author_info(browser.html, browser.url, account_id)

        # Save author to database immediately
        if self.db_engine and author:
            saved = self.db.save_author(author)
            if saved:
                logger.debug(f"[{account_id}] Author saved to database")
            else:
                logger.warning(f"[{account_id}] Failed to save author to database")

        # Step 8: Extract notes with auto-reconnection support
        notes: list[XiaohongshuNote] = []

        # Load existing note IDs from database to skip already scraped notes
        skip_days = int(task.payload.get("skip_existing_days", self.skip_existing_days))
        skip_existing = task.payload.get("skip_existing_notes", self.skip_existing_notes)
        if skip_existing and self.db_engine:
            processed_note_ids = self.db.load_existing_note_ids(
                account_id, days=skip_days
            )
        else:
            processed_note_ids = set()

        max_reconnect_attempts = 3
        reconnect_attempt = 0

        while reconnect_attempt <= max_reconnect_attempts:
            try:
                new_notes = self._extract_notes(
                    browser, account_id, max_notes, processed_note_ids
                )
                notes.extend(new_notes)
                break  # Success, exit loop

            except BrowserDisconnectedError as e:
                reconnect_attempt += 1
                processed_note_ids = e.processed_ids
                logger.warning(
                    f"[{account_id}] Reconnection attempt {reconnect_attempt}/{max_reconnect_attempts}, "
                    f"already processed {len(processed_note_ids)} note IDs"
                )

                if reconnect_attempt > max_reconnect_attempts:
                    logger.error(
                        f"[{account_id}] Max reconnection attempts reached. "
                        f"Returning {len(notes)} notes."
                    )
                    break

                # Try to reconnect by navigating back to profile
                try:
                    logger.info(f"[{account_id}] Attempting to reconnect...")
                    time.sleep(5)  # Wait before reconnecting

                    # Re-navigate to profile
                    profile_url = f"https://www.xiaohongshu.com/user/profile/{account_id}"
                    browser.get(profile_url)
                    time.sleep(5)

                    # Verify page loaded
                    if "xiaohongshu" in browser.url:
                        logger.info(f"[{account_id}] Reconnection successful")
                    else:
                        logger.error(f"[{account_id}] Reconnection failed - wrong URL")
                        break

                except Exception as reconnect_error:
                    logger.error(
                        f"[{account_id}] Reconnection failed: {reconnect_error}"
                    )
                    break

        if not notes:
            logger.warning(f"[{account_id}] No notes found")

        # Step 9: Count images downloaded
        images_downloaded = sum(len(note.image_paths) for note in notes)

        # Note: Author and notes are already saved in real-time during extraction

        # Step 9: Update author's note_count from actual scraped notes in database
        if self.db_engine and author:
            self.db.update_author_note_count(author.user_id)

        logger.info(
            f"[{account_id}] Complete: {len(notes)} notes, {images_downloaded} images"
        )

        return XiaohongshuResult(
            success=len(notes) > 0,
            task_key=task.task_key,
            task_type=self.task_type,
            account_id=account_id,
            author=author,
            notes=notes,
            notes_count=len(notes),
            images_downloaded=images_downloaded,
            s3_uploaded=self.s3_enabled and images_downloaded > 0,
        )

    def _perform_search(self, browser: Any, account_id: str) -> bool:
        """Perform search using search box instead of URL navigation.

        XHS homepage search bar re-renders the DOM on click, so element
        references obtained before clicking become stale. This method uses
        browser.actions (keyboard events sent to the focused element) for
        all typing and submission to avoid stale reference issues.

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID to search for.

        Returns:
            True if search was performed successfully via search box.
        """
        try:
            # Phase 1: Click the search area to activate it
            search_area_selectors = [
                "xpath://input[contains(@placeholder, '搜索')]",
                "xpath://div[contains(@class, 'search-input')]",
                "xpath://div[contains(@id, 'search')]",
                "css:#search-input",
                "css:.search-input",
            ]

            clicked = False
            for selector in search_area_selectors:
                try:
                    elem = browser.ele(selector, timeout=3)
                    if elem:
                        elem.click()
                        logger.info(f"[{account_id}] Clicked search area: {selector}")
                        clicked = True
                        time.sleep(2)
                        break
                except Exception:
                    continue

            if not clicked:
                logger.warning(f"[{account_id}] Search area not found")
                return False

            self._save_page_screenshot(browser, account_id, "search_area_clicked")

            # Phase 2: Type via CDP keyboard actions (avoids stale DOM references)
            # XHS re-renders the input on click, so .input() on the old
            # element ref doesn't work. browser.actions uses CDP
            # Input.dispatchKeyEvent which sends to the currently focused element.
            # Select all + delete to clear any existing text
            browser.actions.key_down("ctrl").key_down("a").key_up("a").key_up("ctrl")
            time.sleep(0.2)
            browser.actions.key_down("Backspace").key_up("Backspace")
            time.sleep(0.2)

            # Type the search query with small interval between characters
            browser.actions.type(account_id, interval=0.05)
            time.sleep(1)

            self._save_page_screenshot(browser, account_id, "search_typed")

            # Phase 3: Submit with Enter via CDP keyboard action
            browser.actions.type("\n")
            logger.info(f"[{account_id}] Search submitted via keyboard")
            time.sleep(5)

            self._save_page_screenshot(browser, account_id, "search_submitted")

            # Verify we're on a search results page
            current_url = browser.url or ""
            if "search_result" in current_url or "search" in current_url:
                logger.info(f"[{account_id}] On search results page: {current_url}")
                return True

            logger.warning(f"[{account_id}] Not on search results page after submit: {current_url}")
            return False

        except Exception as e:
            logger.warning(f"[{account_id}] Search interaction failed: {e}")
            self._save_page_screenshot(browser, account_id, "search_error")
            return False

    def _navigate_to_user_profile(
        self, browser: Any, account_id: str
    ) -> tuple[str | None, Any]:
        """Navigate to user profile by clicking UI elements (no URL navigation).

        Flow: Click first search result (note) -> Click author link on note page

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID to search for.

        Returns:
            Tuple of (profile_url, browser/tab).
            profile_url is "LOGIN_REQUIRED" if login is detected, None if not found.
        """
        try:
            time.sleep(3)

            # Check for login requirement (may load with delay)
            if self._detect_login_required(browser):
                logger.warning(f"[{account_id}] Login required detected in search results")
                return ("LOGIN_REQUIRED", browser)

            self._save_page_screenshot(browser, account_id, "search_results")

            # Save search results HTML for debugging selector issues
            self._save_page_html(browser, account_id, "search_results")

            # Step 1: Click the user card at the top of search results.
            # When searching a user's XHS number, a user card with class "onebox" appears
            # above note results. Structure:
            #   div.onebox > a[href="/user/profile/..."] > div.user-item-box
            # IMPORTANT: avoid matching sidebar "我" links which also have /user/profile/.
            user_card_selectors = [
                # Primary: the onebox user card link (most reliable)
                "xpath://div[contains(@class, 'onebox')]//a[contains(@href, '/user/profile/')]",
                # Fallback: user-item-box container
                "xpath://div[contains(@class, 'user-item-box')]//ancestor::a[contains(@href, '/user/profile/')]",
                # Fallback: user card identified by 小红书号 text
                "xpath://span[contains(@class, 'user-desc') and contains(text(), '小红书号')]//ancestor::a[contains(@href, '/user/profile/')]",
            ]

            original_tabs = browser.tab_ids
            clicked = False

            for selector in user_card_selectors:
                try:
                    elements = browser.eles(selector)
                    if elements:
                        user_card = elements[0]
                        logger.info(f"[{account_id}] Clicking user card: {selector}")
                        user_card.click()
                        time.sleep(3)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                logger.warning(f"[{account_id}] No user card found in search results")
                self._save_page_screenshot(browser, account_id, "no_user_card")
                return (None, browser)

            # Step 2: Check navigation result — new tab or same tab
            url, new_browser = self._switch_to_new_tab_if_opened(browser, original_tabs, account_id)

            # Check new tab
            if url and "/user/profile/" in url:
                if self._verify_user_id(new_browser, account_id):
                    return (url, new_browser)
                else:
                    logger.error(f"[{account_id}] User ID mismatch on profile page")
                    return ("USER_ID_MISMATCH", new_browser)

            # Check same-tab navigation
            current_url = browser.url or ""
            if "/user/profile/" in current_url:
                if self._verify_user_id(browser, account_id):
                    return (current_url, browser)
                else:
                    logger.error(f"[{account_id}] User ID mismatch on profile page")
                    return ("USER_ID_MISMATCH", browser)

            # If we got somewhere but not a profile page
            target = new_browser if url else browser
            logger.error(
                f"[{account_id}] Clicked user card but didn't reach profile. "
                f"URL: {target.url}"
            )
            self._save_page_screenshot(target, account_id, "not_profile_after_card_click")
            return (None, target)

        except Exception as e:
            logger.error(f"[{account_id}] Error navigating to profile: {e}")
            return (None, browser)

    def _switch_to_new_tab_if_opened(
        self, browser: Any, original_tabs: list, account_id: str
    ) -> tuple[str | None, Any]:
        """Check if a new tab was opened and switch to it.

        Args:
            browser: DrissionPage browser instance.
            original_tabs: List of tab IDs before clicking.
            account_id: Account ID for logging.

        Returns:
            Tuple of (URL, browser/tab object to use for subsequent operations).
            Returns (None, browser) if no navigation occurred.
        """
        try:
            current_tabs = browser.tab_ids

            # Check if new tab opened
            if len(current_tabs) > len(original_tabs):
                # Find the new tab
                new_tabs = [t for t in current_tabs if t not in original_tabs]
                if new_tabs:
                    new_tab_id = new_tabs[0]
                    logger.info(f"[{account_id}] New tab opened, switching to it")
                    # Use get_tab() to get the new tab object
                    new_tab = browser.get_tab(new_tab_id)
                    time.sleep(2)
                    logger.info(f"[{account_id}] Switched to new tab: {new_tab.url}")
                    # Close the old tabs to clean up
                    for old_tab_id in original_tabs:
                        try:
                            browser.close_tabs(old_tab_id)
                        except Exception:
                            pass
                    return (new_tab.url, new_tab)

            # No new tab, check if current page is profile
            if "/user/profile/" in browser.url:
                logger.info(f"[{account_id}] Successfully navigated to profile (same tab)")
                return (browser.url, browser)

            return (None, browser)

        except Exception as e:
            logger.debug(f"[{account_id}] Error checking tabs: {e}")
            return (None, browser)

    def _verify_user_id(self, browser: Any, expected_id: str) -> bool:
        """Verify that the opened profile matches the expected user ID.

        Checks against multiple identifiers: Xiaohongshu number (小红书号),
        URL user_id, and display name. The expected_id may be any of these
        (e.g. a numeric XHS ID like "5f52707d..." or a display name like
        "1jerseyaday").

        Args:
            browser: DrissionPage browser instance.
            expected_id: Expected identifier (XHS number, user_id, or display name).

        Returns:
            True if any identifier matches, False otherwise.
        """
        try:
            html = browser.html
            expected_lower = expected_id.lower()

            # 1. Check Xiaohongshu number (小红书号)
            xhs_id_patterns = [
                r'小红书号[：:]\s*([^\s<]+)',
                r'"redId"\s*:\s*"?([^"<\s]+)"?',
                r'"red_id"\s*:\s*"?([^"<\s]+)"?',
                r'data-redid="([^"]+)"',
            ]
            for pattern in xhs_id_patterns:
                match = re.search(pattern, html)
                if match:
                    found_id = match.group(1).strip()
                    if found_id.lower() == expected_lower:
                        logger.info(f"[{expected_id}] User ID verified via XHS number: {found_id}")
                        return True
                    logger.debug(f"[{expected_id}] XHS number on page: {found_id}")

            # 2. Check URL user_id (e.g. /user/profile/5f52707d000000000101da8a)
            url = browser.url or ""
            url_match = re.search(r'/user/profile/([^/?#]+)', url)
            if url_match:
                url_user_id = url_match.group(1)
                if url_user_id.lower() == expected_lower:
                    logger.info(f"[{expected_id}] User ID verified via URL: {url_user_id}")
                    return True
                logger.debug(f"[{expected_id}] URL user_id: {url_user_id}")

            # 3. Check display name on profile page
            name_patterns = [
                r'"nickname"\s*:\s*"([^"]+)"',
                r'class="user-name[^"]*"[^>]*>([^<]+)<',
                r'<title>([^<]+)</title>',
            ]
            for pattern in name_patterns:
                match = re.search(pattern, html)
                if match:
                    found_name = match.group(1).strip()
                    if found_name.lower() == expected_lower:
                        logger.info(f"[{expected_id}] User ID verified via display name: {found_name}")
                        return True
                    logger.debug(f"[{expected_id}] Display name on page: {found_name}")

            # 4. If we're on a /user/profile/ page and the expected_id is not a
            #    numeric XHS ID, accept it — the search result click already
            #    matched the user visually.
            if "/user/profile/" in url and not expected_id.isdigit():
                logger.info(
                    f"[{expected_id}] On profile page ({url}), accepting non-numeric ID match"
                )
                return True

            logger.warning(f"[{expected_id}] Could not verify user identity on profile page")
            return False

        except Exception as e:
            logger.debug(f"[{expected_id}] Error verifying user ID: {e}")
            return False

    def _extract_notes(
        self,
        browser: Any,
        account_id: str,
        max_notes: int,
        existing_note_ids: set[str] | None = None,
    ) -> list[XiaohongshuNote]:
        """Extract notes from the user's profile page by clicking on thumbnails.

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID being scraped.
            max_notes: Maximum notes to extract.
            existing_note_ids: Set of note IDs already processed (for reconnection).

        Returns:
            List of extracted XiaohongshuNote objects.

        Raises:
            BrowserDisconnectedError: If browser connection is lost.
        """
        notes: list[XiaohongshuNote] = []
        # Include existing note IDs to skip already processed notes (from DB or reconnection)
        processed_note_ids: set[str] = set(existing_note_ids) if existing_note_ids else set()
        if existing_note_ids:
            logger.info(
                f"[{account_id}] Will skip {len(existing_note_ids)} already processed note IDs"
            )

        # XHS profile uses aggressive virtualization: the DOM only ever holds ~12-32
        # note-item nodes even when the user has 1500+ notes; scrollHeight grows as
        # more batches are lazy-loaded while nodes outside the viewport are recycled.
        # So we can't rely on "all note-items in DOM" — we have to scroll and process
        # notes as they pass through the visible window, terminating only when
        # scrollHeight stops growing and we've reached the bottom of the page.
        selector = "xpath://section[contains(@class, 'note-item')]"
        SCROLL_HEIGHT_STABLE_THRESHOLD = 8
        IDLE_ITERATIONS_BEFORE_STOP = 20
        MAX_ITERATIONS = 4000

        def _page_state() -> tuple[int, int, int, int]:
            try:
                info = browser.run_js(
                    "return ["
                    "window.scrollY, "
                    "document.body.scrollHeight, "
                    "window.innerHeight, "
                    "document.querySelectorAll('section.note-item').length"
                    "];"
                )
                if info and len(info) >= 4:
                    return int(info[0]), int(info[1]), int(info[2]), int(info[3])
            except Exception as e:
                if self._is_connection_error(e):
                    raise
            return 0, 0, 800, 0

        try:
            iteration = 0
            last_scroll_height = 0
            stable_height_count = 0
            last_progress_iter = 0

            while len(notes) < max_notes and iteration < MAX_ITERATIONS:
                iteration += 1

                scroll_y, scroll_h, viewport_h, dom_count = _page_state()

                if iteration == 1 or iteration % 10 == 0:
                    logger.info(
                        f"[{account_id}] iter={iteration} notes={len(notes)} "
                        f"processed={len(processed_note_ids)} "
                        f"scrollY={scroll_y} scrollH={scroll_h} "
                        f"viewportH={viewport_h} domCount={dom_count}"
                    )

                try:
                    note_elements = browser.eles(selector)
                except Exception as e:
                    if self._is_connection_error(e):
                        raise
                    note_elements = []

                extracted_this_iter = False
                for idx, element in enumerate(note_elements):
                    if len(notes) >= max_notes:
                        break

                    note_id = self.parser.extract_note_id_from_element(element)
                    if not note_id or note_id in processed_note_ids:
                        continue

                    try:
                        note = self._extract_single_note_by_click(
                            browser, element, idx, account_id
                        )
                    except BrowserDisconnectedError:
                        raise
                    except Exception as e:
                        if self._is_connection_error(e):
                            raise BrowserDisconnectedError(
                                task_key=account_id,
                                items_extracted=len(notes),
                                processed_ids=processed_note_ids,
                            )
                        logger.warning(
                            f"[{account_id}] Error extracting note {idx}: {e}"
                        )
                        processed_note_ids.add(note_id)
                        extracted_this_iter = True
                        break

                    # Always mark visited — a None return means the element was
                    # unclickable or the detail failed; retrying same DOM node
                    # doesn't help and infinite loops the scraper.
                    processed_note_ids.add(note_id)
                    extracted_this_iter = True

                    if note:
                        if note.note_id and note.note_id != note_id:
                            processed_note_ids.add(note.note_id)
                        notes.append(note)
                        if self.db_engine:
                            saved = self.db.save_note(note)
                            save_status = "saved" if saved else "save failed"
                        else:
                            save_status = "db not configured"
                        logger.info(
                            f"[{account_id}] Extracted note {len(notes)}/{max_notes}: "
                            f"{note.note_id} ({save_status})"
                        )
                    else:
                        logger.debug(
                            f"[{account_id}] Note {note_id} returned None; marked visited"
                        )

                    # Process only one note per iteration — the DOM may be stale
                    # after modal close, so re-query on the next loop.
                    break

                if extracted_this_iter:
                    last_progress_iter = iteration
                    time.sleep(0.6)
                    continue

                # No unprocessed note in the current window — scroll to reveal more.
                if scroll_h == last_scroll_height:
                    stable_height_count += 1
                else:
                    stable_height_count = 0
                    last_scroll_height = scroll_h

                near_bottom = (scroll_y + viewport_h + 100) >= scroll_h
                idle_iterations = iteration - last_progress_iter
                if (
                    stable_height_count >= SCROLL_HEIGHT_STABLE_THRESHOLD
                    and near_bottom
                    and idle_iterations >= IDLE_ITERATIONS_BEFORE_STOP
                ):
                    logger.info(
                        f"[{account_id}] Reached bottom: scrollH stable at {scroll_h} "
                        f"for {stable_height_count} iters, idle for {idle_iterations} iters"
                    )
                    break

                step = max(400, int(viewport_h * 0.8))
                try:
                    browser.run_js(
                        f"window.scrollBy({{top: {step}, behavior: 'smooth'}})"
                    )
                except Exception as e:
                    if self._is_connection_error(e):
                        raise
                    try:
                        browser.run_js(f"window.scrollBy(0, {step})")
                    except Exception:
                        pass

                time.sleep(0.7)

            logger.info(f"[{account_id}] Extracted {len(notes)} notes total")
            return notes

        except BrowserDisconnectedError:
            # Re-raise to allow reconnection handling in scrape()
            raise

        except Exception as e:
            error_msg = str(e).lower()
            if "disconnected" in error_msg or "connection" in error_msg:
                logger.error(
                    f"[{account_id}] Browser connection lost. "
                    f"Will attempt reconnection."
                )
                raise BrowserDisconnectedError(
                    task_key=account_id,
                    items_extracted=len(notes),
                    processed_ids=processed_note_ids,
                )
            else:
                logger.error(f"[{account_id}] Error extracting notes: {e}")
            return notes

    @staticmethod
    def _is_connection_error(err: BaseException) -> bool:
        msg = str(err).lower()
        return "disconnected" in msg or "connection" in msg

    def _extract_single_note_by_click(
        self, browser: Any, element: Any, index: int, account_id: str
    ) -> XiaohongshuNote | None:
        """Extract a single note by clicking on its thumbnail.

        Args:
            browser: DrissionPage browser instance.
            element: The clickable note element.
            index: Index of the note for logging.
            account_id: Account ID for reference.

        Returns:
            XiaohongshuNote or None if extraction fails.
        """
        try:
            # Check if element has valid position - if not, skip it
            # (it will be processed in a future scroll iteration when it becomes visible)
            try:
                rect = browser.run_js(
                    "var r = arguments[0].getBoundingClientRect(); "
                    "return {top: r.top, left: r.left, width: r.width, height: r.height};",
                    element
                )
                if not rect or rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
                    # Element not rendered yet, skip it for now
                    logger.debug(f"[{account_id}] Skipping element {index} - not rendered yet")
                    return None

                # If element is outside viewport, scroll to it
                if rect.get("top", 0) < 0 or rect.get("top", 0) > 800:
                    browser.run_js(
                        "arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});",
                        element
                    )
                    time.sleep(0.3)
            except Exception as e:
                logger.debug(f"[{account_id}] Cannot check element {index} position: {e}")
                return None

            # Now try to get note ID from href (after lazy loading)
            note_id = None
            note_url = None

            # Try multiple strategies to extract note_id
            note_id_pattern = re.compile(r"[a-f0-9]{24}")

            # Strategy A: Direct href attribute
            try:
                href = element.attr("href")
                if href and "/explore/" in href:
                    extracted_id = href.split("/explore/")[-1].split("?")[0]
                    if extracted_id and note_id_pattern.match(extracted_id):
                        note_id = extracted_id
            except Exception:
                pass

            # Strategy B: Child anchor element's href
            if not note_id:
                try:
                    child_links = element.eles("tag:a", timeout=0.3)
                    for link in child_links:
                        link_href = link.attr("href")
                        if link_href and "/explore/" in link_href:
                            extracted_id = link_href.split("/explore/")[-1].split("?")[0]
                            if extracted_id and note_id_pattern.match(extracted_id):
                                note_id = extracted_id
                                break
                except Exception:
                    pass

            # Strategy C: Check element's outer HTML for note ID pattern
            if not note_id:
                try:
                    outer_html = element.html
                    if outer_html:
                        match = re.search(r"(?:/explore/|/discovery/item/)([a-f0-9]{24})", outer_html)
                        if match:
                            note_id = match.group(1)
                except Exception:
                    pass

            # Build URL if we have a valid note_id
            if note_id and note_id_pattern.match(note_id):
                note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
            else:
                # Fallback ID - no direct URL navigation possible
                note_id = f"note_{index}_{int(time.time())}"

            logger.debug(f"[{account_id}] Clicking on note {index}: {note_id}")

            # Dismiss any overlays that might block the click
            self._dismiss_overlays(browser)

            # Click on the note thumbnail to open detail modal
            try:
                element.click()
            except Exception as e:
                logger.warning(f"[{account_id}] Failed to click note {index}: {e}")
                return None

            # Wait for modal/page to load
            time.sleep(3)
            self._save_page_screenshot(browser, account_id, f"note_{note_id}")
            html_path = self._save_page_html(browser, account_id, f"note_{note_id}")

            # Check for login
            if self._detect_login_required(browser):
                self._close_modal(browser)
                return None

            # Get current URL (might have changed if opened in new page)
            current_url = browser.url
            if "/explore/" in current_url:
                # Extract note_id from URL if available
                url_note_id = current_url.split("/explore/")[-1].split("?")[0]
                if url_note_id:
                    note_id = url_note_id

            note = XiaohongshuNote(
                note_id=note_id,
                source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
                author_id=account_id,
                source_html_path=html_path,
            )

            # Extract data using browser selectors (more reliable than regex)
            # Title - from note detail title element
            try:
                title_ele = browser.ele("css:#detail-title", timeout=2)
                if title_ele:
                    note.title = title_ele.text.strip()[:500]
            except Exception:
                pass

            # Content/Description - from note detail description
            try:
                desc_ele = browser.ele("css:#detail-desc", timeout=2)
                if desc_ele:
                    # Get text content, preserving line breaks
                    note.content = desc_ele.text.strip()[:5000]
            except Exception:
                pass

            # Author name - from author info section
            try:
                # Try multiple selectors for author name
                author_selectors = [
                    "css:.author-wrapper .username",
                    "css:.author-container .username",
                    "css:.user-name",
                    "css:.name",
                    "xpath://div[contains(@class, 'author')]//span[contains(@class, 'name')]",
                    "xpath://a[contains(@class, 'name')]",
                ]
                for sel in author_selectors:
                    try:
                        author_ele = browser.ele(sel, timeout=1)
                        if author_ele and author_ele.text.strip():
                            note.author_name = author_ele.text.strip()
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # Extract publish date
            try:
                date_selectors = [
                    "css:.bottom-container span.date",
                    "css:.note-detail-mask .date",
                    "css:.feed-detail .date",
                    "css:.bottom-container .date",
                    "css:.publish-date",
                    "css:span.date",
                ]
                for sel in date_selectors:
                    try:
                        date_ele = browser.ele(sel, timeout=2)
                        if not date_ele:
                            continue
                        date_text = date_ele.text.strip()
                        if not date_text:
                            continue
                        if any(loc in date_text for loc in ["北京", "上海", "广东", "浙江", "江苏", "IP"]):
                            continue
                        parsed_date = self.parser.parse_relative_date(date_text)
                        if parsed_date:
                            note.created_at = parsed_date
                            logger.debug(f"[{account_id}] Parsed date '{date_text}' -> {parsed_date}")
                            break
                    except Exception:
                        continue
                # Fallback: extract date from raw HTML
                if not note.created_at:
                    try:
                        html = browser.html or ""
                        date_match = re.search(
                            r'class="date"[^>]*>(\d{4}-\d{2}-\d{2})', html
                        )
                        if not date_match:
                            date_match = re.search(
                                r'class="date"[^>]*>(\d{1,2}-\d{2})', html
                            )
                        if not date_match:
                            date_match = re.search(
                                r'class="date"[^>]*>([^<]{1,30})</s*', html
                            )
                        if date_match:
                            date_text = date_match.group(1).strip()
                            parsed_date = self.parser.parse_relative_date(date_text)
                            if parsed_date:
                                note.created_at = parsed_date
                                logger.info(f"[{account_id}] Date from HTML fallback: '{date_text}' -> {parsed_date}")
                    except Exception:
                        pass
            except Exception:
                pass

            # Extract tags from hashtags in content
            tags = []
            try:
                # Try multiple selectors for tag elements
                tag_selectors = [
                    "css:#detail-desc a.tag",
                    "css:.note-content a.tag",
                    "css:a[href*='/search_result?keyword=']",
                    "css:a[href*='search'][class*='tag']",
                ]
                for sel in tag_selectors:
                    try:
                        tag_elements = browser.eles(sel)
                        if tag_elements:
                            for tag_ele in tag_elements:
                                tag_text = tag_ele.text.strip()
                                if tag_text.startswith('#'):
                                    tag_text = tag_text[1:]
                                # Filter out invalid tags (CSS, empty, too long)
                                if tag_text and len(tag_text) < 50 and not any(c in tag_text for c in ['{', '}', ':', ';', '.']):
                                    tags.append(tag_text)
                            if tags:
                                break
                    except Exception:
                        continue
            except Exception:
                pass

            # Fallback: extract from #hashtag patterns in content text
            if not tags and note.content:
                try:
                    hashtag_matches = re.findall(r'#([^\s#\[\]{}()<>]{1,30})', note.content)
                    for match in hashtag_matches:
                        # Filter out CSS-like content
                        if not any(c in match for c in ['{', '}', ':', ';']):
                            tags.append(match)
                except Exception:
                    pass

            note.tags = list(dict.fromkeys(tags))[:20]

            # Extract engagement metrics using browser selectors
            # IMPORTANT: Must use selectors that target the detail modal, not the background feed
            # The detail modal has class 'note-detail-mask' or 'feed-detail'
            try:
                # Like count - target the detail modal specifically
                # The engage-bar inside the detail modal contains the correct counts
                like_selectors = [
                    # Detail modal specific selectors (highest priority)
                    "css:.note-detail-mask .engage-bar .like-wrapper .count",
                    "css:.feed-detail .engage-bar .like-wrapper .count",
                    "css:.note-detail-mask .like-wrapper .count",
                    "css:.feed-detail .like-wrapper .count",
                    # Fallback selectors
                    "css:.engage-bar-style .like-wrapper .count",
                ]
                for sel in like_selectors:
                    try:
                        like_ele = browser.ele(sel, timeout=1)
                        if like_ele and like_ele.text.strip():
                            like_text = like_ele.text.strip()
                            logger.debug(f"Found like count with selector '{sel}': {like_text}")
                            note.like_count = self.parser.parse_count(like_text)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            try:
                # Collect/favorite count - target the detail modal specifically
                collect_selectors = [
                    # Detail modal specific selectors (highest priority)
                    "css:.note-detail-mask .engage-bar .collect-wrapper .count",
                    "css:.feed-detail .engage-bar .collect-wrapper .count",
                    "css:.note-detail-mask .collect-wrapper .count",
                    "css:.feed-detail .collect-wrapper .count",
                    # Fallback selectors
                    "css:.engage-bar-style .collect-wrapper .count",
                ]
                for sel in collect_selectors:
                    try:
                        collect_ele = browser.ele(sel, timeout=1)
                        if collect_ele and collect_ele.text.strip():
                            collect_text = collect_ele.text.strip()
                            logger.debug(f"Found collect count with selector '{sel}': {collect_text}")
                            note.collect_count = self.parser.parse_count(collect_text)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            try:
                # Comment count - target the detail modal specifically
                comment_selectors = [
                    # Detail modal specific selectors (highest priority)
                    "css:.note-detail-mask .engage-bar .chat-wrapper .count",
                    "css:.feed-detail .engage-bar .chat-wrapper .count",
                    "css:.note-detail-mask .chat-wrapper .count",
                    "css:.feed-detail .chat-wrapper .count",
                    # Fallback selectors
                    "css:.engage-bar-style .chat-wrapper .count",
                ]
                for sel in comment_selectors:
                    try:
                        comment_ele = browser.ele(sel, timeout=1)
                        if comment_ele and comment_ele.text.strip():
                            comment_text = comment_ele.text.strip()
                            logger.debug(f"Found comment count with selector '{sel}': {comment_text}")
                            note.comment_count = self.parser.parse_count(comment_text)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # Log warning if metrics extraction failed
            if not note.like_count:
                logger.warning(f"[{account_id}] Failed to extract like count for note {note.note_id}")
            if not note.collect_count:
                logger.warning(f"[{account_id}] Failed to extract collect count for note {note.note_id}")
            if not note.comment_count:
                logger.warning(f"[{account_id}] Failed to extract comment count for note {note.note_id}")

            # Image URLs - will be populated by _download_media_via_context_menu
            # No need to extract here since we get them from the slides directly
            note.image_urls = []

            logger.debug(f"Extracted note: title={note.title[:30] if note.title else 'N/A'}..., author={note.author_name}, tags={len(note.tags)}, likes={note.like_count}")

            # Download images/videos via right-click context menu while modal is open
            downloaded = self._download_media_via_context_menu(browser, note, account_id)
            logger.info(f"[{account_id}] Downloaded {downloaded} media files via context menu for note {note.note_id}")

            # Extract comments if configured
            if self.extract_comments and note.comment_count and note.comment_count > 0:
                try:
                    comments = self._extract_comments(
                        browser, account_id, self.max_comments_per_note
                    )
                    note.comments = comments
                    logger.info(
                        f"[{account_id}] Extracted {len(comments)} comments "
                        f"with {sum(len(c.replies) for c in comments)} replies for note {note.note_id}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{account_id}] Comment extraction failed for note {note.note_id}: {e}",
                        exc_info=True,
                    )

            # Close the note modal/go back
            self._close_modal(browser)
            time.sleep(1)

            return note

        except Exception as e:
            logger.warning(f"[{account_id}] Error extracting note {index}: {e}")
            # Try to close modal on error
            try:
                self._close_modal(browser)
            except Exception:
                pass
            return None

    def _extract_comments(
        self,
        browser: Any,
        account_id: str,
        max_comments: int = 100,
    ) -> list[XiaohongshuComment]:
        """Extract comments from the note detail page.

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID for logging.
            max_comments: Maximum number of comments to extract.

        Returns:
            List of extracted XiaohongshuComment objects.
        """
        comments: list[XiaohongshuComment] = []

        try:
            # First scroll the right panel (interaction area) to trigger comment lazy-loading.
            # XHS note modals have a scrollable right panel containing comments.
            right_panel_selectors = [
                "css:.interaction-container",
                "css:.note-scroller",
                "css:.note-detail-mask .right-container",
                "css:.feed-detail .right-container",
                "css:.comments-el",
            ]
            for sel in right_panel_selectors:
                try:
                    panel = browser.ele(sel, timeout=1)
                    if panel:
                        browser.run_js(
                            "arguments[0].scrollTop = arguments[0].scrollHeight",
                            panel,
                        )
                        time.sleep(1)
                        browser.run_js("arguments[0].scrollTop = 0", panel)
                        time.sleep(0.5)
                        logger.debug(f"[{account_id}] Scrolled right panel: {sel}")
                        break
                except Exception:
                    continue

            # Wait for comment section to load
            time.sleep(2)

            # Scroll to comment section to trigger loading
            try:
                comment_container_selectors = [
                    "css:.comments-container",
                    "css:.comment-list",
                    "css:[class*='comment'][class*='container']",
                    "css:.note-detail-mask .comments",
                    "css:.feed-detail .comments",
                ]
                for sel in comment_container_selectors:
                    try:
                        container = browser.ele(sel, timeout=2)
                        if container:
                            browser.run_js(
                                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'start'})",
                                container,
                            )
                            time.sleep(1)
                            break
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"[{account_id}] Could not scroll to comments: {e}")

            # Check how many comment-items are in the DOM via JS (bypasses DrissionPage element search)
            dom_count = 0
            try:
                dom_count = browser.run_js(
                    "return document.querySelectorAll('.comment-item').length"
                ) or 0
                logger.debug(f"[{account_id}] JS querySelectorAll found {dom_count} .comment-item elements")
            except Exception:
                pass

            # Find comment items
            comment_item_selectors = [
                "css:.comment-item:not(.comment-item-sub)",
                "css:.comment-item",
                "css:.parent-comment",
                "css:[class*='comment-item']",
                "css:.comments-container > div",
                "xpath://div[contains(@class, 'comment') and contains(@class, 'item')]",
            ]

            comment_elements = []
            for sel in comment_item_selectors:
                try:
                    elements = browser.eles(sel)
                    if elements:
                        comment_elements = elements
                        logger.debug(
                            f"[{account_id}] Found {len(elements)} comments with selector: {sel}"
                        )
                        break
                except Exception:
                    continue

            if not comment_elements:
                logger.debug(f"[{account_id}] No comments found initially, waiting and retrying...")
                time.sleep(3)

                # Retry scrolling the right panel before searching again
                for sel in right_panel_selectors:
                    try:
                        panel = browser.ele(sel, timeout=1)
                        if panel:
                            browser.run_js(
                                "arguments[0].scrollTop = arguments[0].scrollHeight / 2",
                                panel,
                            )
                            time.sleep(1)
                            break
                    except Exception:
                        continue

                for sel in comment_item_selectors:
                    try:
                        elements = browser.eles(sel)
                        if elements:
                            comment_elements = elements
                            logger.debug(
                                f"[{account_id}] Found {len(elements)} comments on retry with: {sel}"
                            )
                            break
                    except Exception:
                        continue

            if not comment_elements:
                logger.info(
                    f"[{account_id}] No comment elements found after retry "
                    f"(JS DOM count was {dom_count})"
                )
                return comments

            # Scroll to load more comments if needed
            # Add 20% buffer to account for empty/invalid comment elements
            target_elements = int(max_comments * 1.2)
            scroll_attempts = 0
            max_scroll_attempts = max_comments // 5 + 10  # More scroll attempts
            no_new_comments_count = 0

            while len(comment_elements) < target_elements and scroll_attempts < max_scroll_attempts:
                prev_count = len(comment_elements)
                # Try to load more comments by scrolling with mouse wheel simulation
                try:
                    # Find the comments container to scroll within
                    comments_container = None
                    container_selectors = [
                        "css:.comments-container",
                        "css:.list-container",
                        "css:.comments-el",
                        "css:.interaction-container",
                    ]
                    for sel in container_selectors:
                        try:
                            elem = browser.ele(sel, timeout=1)
                            if elem:
                                comments_container = elem
                                break
                        except Exception:
                            continue

                    if comments_container:
                        # Move mouse to comments area and use mouse wheel scroll
                        try:
                            # Hover over the comments container
                            comments_container.hover()
                            time.sleep(0.3)

                            # Simulate mouse wheel scroll down (multiple times for more scroll)
                            for _ in range(3):
                                browser.actions.scroll(delta_y=500)
                                time.sleep(0.3)

                            logger.debug(
                                f"[{account_id}] Mouse wheel scroll in comments area"
                            )
                        except Exception as e:
                            logger.debug(f"[{account_id}] Mouse scroll failed: {e}")

                            # Fallback: try using the element's scroll method
                            try:
                                comments_container.scroll.to_bottom()
                            except Exception:
                                pass

                    # Additional: scroll the last comment into view
                    try:
                        last_comment = comment_elements[-1] if comment_elements else None
                        if last_comment:
                            last_comment.scroll.to_see()
                            time.sleep(0.3)
                            # Hover and scroll more
                            last_comment.hover()
                            browser.actions.scroll(delta_y=300)
                    except Exception:
                        pass

                    # Get current comment count for logging
                    current_count = browser.run_js(
                        "return document.querySelectorAll('.comment-item').length"
                    )
                    logger.debug(
                        f"[{account_id}] Scroll attempt {scroll_attempts + 1}: "
                        f"comments in DOM = {current_count}"
                    )
                    time.sleep(2.0)  # Wait for content to load

                    # Re-find comments
                    for sel in comment_item_selectors:
                        try:
                            elements = browser.eles(sel)
                            if elements and len(elements) > len(comment_elements):
                                comment_elements = elements
                                logger.debug(
                                    f"[{account_id}] Loaded more comments: {prev_count} -> {len(elements)}"
                                )
                                break
                        except Exception:
                            continue

                    # Check if we loaded new comments
                    if len(comment_elements) == prev_count:
                        no_new_comments_count += 1
                        if no_new_comments_count >= 3:
                            logger.debug(
                                f"[{account_id}] No new comments loaded after 3 attempts, stopping"
                            )
                            break
                    else:
                        no_new_comments_count = 0

                    scroll_attempts += 1
                except Exception as e:
                    logger.debug(f"[{account_id}] Scroll error: {e}")
                    break

            logger.info(
                f"[{account_id}] Found {len(comment_elements)} comment elements after {scroll_attempts} scrolls"
            )

            # Extract each comment until we reach max_comments valid ones
            for idx, comment_elem in enumerate(comment_elements):
                if len(comments) >= max_comments:
                    break
                try:
                    comment = self.parser.extract_single_comment(comment_elem, account_id)
                    if comment:
                        # Expand and extract replies (requires browser interaction)
                        if self.expand_replies:
                            self._expand_and_extract_replies(
                                browser, comment_elem, comment, account_id
                            )
                        comments.append(comment)
                except Exception as e:
                    logger.debug(f"[{account_id}] Error extracting comment {idx}: {e}")
                    continue

            logger.info(
                f"[{account_id}] Extracted {len(comments)} comments "
                f"with {sum(len(c.replies) for c in comments)} total replies"
            )

        except Exception as e:
            logger.warning(f"[{account_id}] Comment extraction failed: {e}")

        return comments

    def _expand_and_extract_replies(
        self,
        _browser: Any,
        comment_element: Any,
        comment: XiaohongshuComment,
        account_id: str,
    ) -> None:
        """Expand and extract replies for a comment.

        Args:
            _browser: DrissionPage browser instance (reserved for future use).
            comment_element: The comment DOM element.
            comment: The XiaohongshuComment to populate with replies.
            account_id: Account ID for logging.
        """
        try:
            # First, try to get reply count from expand button text
            try:
                expand_elem = comment_element.ele(
                    "xpath:.//span[contains(text(), '条回复')]", timeout=0.1
                )
                if expand_elem:
                    count_text = expand_elem.text.strip()
                    count_match = re.search(r'(\d+)', count_text)
                    if count_match:
                        comment.reply_count = int(count_match.group(1))
            except Exception:
                pass

            # Try to expand replies by clicking expand button
            try:
                expand_btn = comment_element.ele(
                    "xpath:.//span[contains(text(), '展开') or contains(text(), '查看')]",
                    timeout=0.1
                )
                if expand_btn:
                    expand_btn.click()
                    time.sleep(0.2)
                    logger.debug(f"[{account_id}] Expanded replies for comment")
            except Exception:
                pass

            # Find reply elements - sub-comments have class "comment-item-sub"
            # Only search within the current comment element to avoid duplicates
            # NOTE: Must specify timeout to avoid 10-second default implicit wait
            try:
                reply_elements = comment_element.eles("css:.comment-item-sub", timeout=0.5)
            except Exception:
                reply_elements = []

            # Extract each reply (sub-comment has same structure as main comment)
            # Use a set to track extracted reply IDs and avoid duplicates
            extracted_reply_ids: set[str] = set()
            for idx, reply_elem in enumerate(reply_elements[:self.max_replies_per_comment]):
                try:
                    reply = self.parser.extract_single_reply(reply_elem, account_id)
                    if reply:
                        # Skip if we've already extracted this reply
                        if reply.reply_id and reply.reply_id in extracted_reply_ids:
                            continue
                        if reply.reply_id:
                            extracted_reply_ids.add(reply.reply_id)
                        comment.replies.append(reply)
                except Exception as e:
                    logger.debug(f"[{account_id}] Error extracting reply {idx}: {e}")
                    continue

            if comment.replies:
                logger.debug(
                    f"[{account_id}] Extracted {len(comment.replies)} replies for comment"
                )

        except Exception as e:
            logger.debug(f"[{account_id}] Error expanding replies: {e}")

    def _download_media_via_context_menu(
        self, browser: Any, note: XiaohongshuNote, account_id: str
    ) -> int:
        """Download images and videos via right-click context menu.

        Args:
            browser: DrissionPage browser instance.
            note: Note object to store downloaded media paths.
            account_id: Account ID for file naming.

        Returns:
            Number of media files downloaded.
        """
        downloaded = 0
        media_index = 0

        # Image selectors
        image_selectors = [
            "xpath://div[contains(@class, 'swiper-slide-active')]//img",
            "xpath://div[contains(@class, 'note-slider')]//img",
            "xpath://div[contains(@class, 'xhs-slider-container')]//img",
        ]

        # Extract image URLs using browser selector, ordered by data-index attribute
        image_data = []  # List of (index, url) tuples
        try:
            # Get all swiper slides
            slides = browser.eles("css:.swiper-slide")
            logger.debug(f"[{account_id}] Found {len(slides)} swiper slides")

            for slide in slides:
                try:
                    # Get data-index attribute for ordering
                    data_index = slide.attr("data-index")
                    if data_index is None:
                        data_index = slide.attr("data-swiper-slide-index")
                    if data_index is None:
                        continue

                    idx = int(data_index)

                    # Find image inside slide
                    img = slide.ele("tag:img", timeout=1)
                    if img:
                        img_url = img.attr("src")
                        if img_url and img_url.startswith("http") and "avatar" not in img_url:
                            image_data.append((idx, img_url))
                            logger.debug(f"[{account_id}] Found image at index {idx}: {img_url[:60]}...")
                except Exception as e:
                    logger.debug(f"[{account_id}] Error processing slide: {e}")
                    continue

        except Exception as e:
            logger.warning(f"[{account_id}] Failed to extract images from slides: {e}")

        # Fallback: get current active image if no slides found
        if not image_data:
            for selector in image_selectors:
                try:
                    img_ele = browser.ele(selector, timeout=2)
                    if img_ele:
                        img_url = img_ele.attr("src")
                        if img_url and img_url.startswith("http"):
                            image_data.append((0, img_url))
                            break
                except Exception:
                    continue

        # Sort by data-index and extract URLs in order
        image_data.sort(key=lambda x: x[0])
        image_urls = [url for _, url in image_data]

        # Deduplicate while preserving order
        image_urls = list(dict.fromkeys(image_urls))[:20]

        # Save image URLs to note for database storage
        note.image_urls = image_urls

        logger.info(f"[{account_id}] Will download {len(image_urls)} images by URL")

        # Get cookies from browser for download
        cookies = {}
        try:
            for cookie in browser.cookies():
                cookies[cookie.get("name", "")] = cookie.get("value", "")
        except Exception:
            pass

        for i, img_url in enumerate(image_urls):
            try:
                logger.debug(f"[{account_id}] Downloading image {i}: {img_url[:80]}...")

                resp = requests.get(
                    img_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                        "Referer": "https://www.xiaohongshu.com/",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                    cookies=cookies,
                    timeout=30,
                )

                if resp.status_code == 200 and len(resp.content) > 1000:
                    # Determine file extension
                    ext = self.parser.detect_image_type(resp.content, resp.headers.get("Content-Type", ""), img_url)
                    safe_account = re.sub(r'[^\w\-]', '_', account_id)
                    filename = f"{safe_account}_{note.note_id}_{media_index}.{ext}"
                    filepath = os.path.join(self.images_dir, filename)

                    with open(filepath, "wb") as f:
                        f.write(resp.content)

                    # Handle S3 upload if enabled
                    final_path = self._handle_upload(filepath)
                    note.image_paths.append(final_path)
                    downloaded += 1
                    media_index += 1
                    logger.debug(f"[{account_id}] Downloaded image {media_index}: {final_path}")
                else:
                    logger.warning(f"[{account_id}] Failed to download image {i}: status={resp.status_code}, size={len(resp.content)}")

            except Exception as e:
                logger.warning(f"[{account_id}] Error downloading image {i}: {e}")
                continue
        return downloaded

    def on_success(self, task: ScraperTask, result: XiaohongshuResult) -> None:
        """Handle successful scrape completion.

        Args:
            task: The completed task.
            result: The scrape result.
        """
        super().on_success(task, result)

        if result.login_required:
            logger.warning(
                f"[{result.account_id}] Scrape incomplete - login required"
            )


class XiaohongshuFollowingScraper(XiaohongshuScraper):
    """Scraper for Xiaohongshu user following lists.

    Inherits from XiaohongshuScraper to reuse search and navigation methods.
    Scrapes the list of users that a given account follows.

    Configuration options (in scraper config):
        max_following: Maximum following users to scrape (default: 1000)
        screenshots_dir: Directory for screenshots (default: "data/xiaohongshu_screenshots")
        login_alert_email: Email to send login alerts to
        smtp_server: SMTP server for alerts (default: "smtp.qq.com")
        smtp_port: SMTP port (default: 465)
        smtp_sender: SMTP sender email
        smtp_password: SMTP password
        wait_for_login: Wait for manual login when required (default: True)
        login_check_interval: Seconds between login checks (default: 5)
        login_timeout: Max seconds to wait for login (default: 300)

    Task payload options:
        max_following: Override max_following for this task
    """

    task_type = "xiaohongshu_following"
    default_delay = (10.0, 20.0)
    requires_browser = True
    cloudflare_protected = False

    # Login indicators - reuse from XiaohongshuScraper plus additional checks
    LOGIN_INDICATORS = XiaohongshuScraper.LOGIN_INDICATORS + [
        "未连接到服务器",
        "点击刷新",
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the Xiaohongshu following scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)

        self.max_following = self.config.get("max_following", 1000)

    def setup(self) -> None:
        """Setup scraper resources and create database tables."""
        super().setup()

        # Initialize database engine for following tables
        if self.database_url:
            try:
                self.db.ensure_following_tables_exist()
                logger.info("Database connection established for following scraper")
            except Exception as e:
                logger.error(f"Failed to initialize database: {e}")

    def _navigate_to_user_profile(
        self, browser: Any, account_id: str
    ) -> tuple[str | None, Any]:
        """Navigate to user profile by clicking first search result directly.

        Unlike parent class, does NOT click on '用户' tab - uses first result directly.

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID to search for.

        Returns:
            Tuple of (profile_url, browser/tab).
        """
        try:
            time.sleep(3)

            # Check for login requirement
            if self._detect_login_required(browser):
                logger.warning(f"[{account_id}] Login required detected in search results")
                return ("LOGIN_REQUIRED", browser)

            self._save_page_screenshot(browser, account_id, "search_results")

            # Get current tab count before clicking
            original_tabs = browser.tab_ids

            # Click on the user card (class "onebox") at the top of search results.
            # IMPORTANT: avoid matching sidebar "我" links.
            result_selectors = [
                # Primary: the onebox user card link (most reliable)
                "xpath://div[contains(@class, 'onebox')]//a[contains(@href, '/user/profile/')]",
                # Fallback: user-item-box container
                "xpath://div[contains(@class, 'user-item-box')]//ancestor::a[contains(@href, '/user/profile/')]",
                # Fallback: user card identified by 小红书号 text
                "xpath://span[contains(@class, 'user-desc') and contains(text(), '小红书号')]//ancestor::a[contains(@href, '/user/profile/')]",
            ]

            for selector in result_selectors:
                try:
                    elements = browser.eles(selector)
                    if elements:
                        first_result = elements[0]
                        logger.info(f"[{account_id}] Found search result, clicking...")
                        first_result.click()
                        time.sleep(3)

                        # Check if a new tab was opened
                        url, new_browser = self._switch_to_new_tab_if_opened(
                            browser, original_tabs, account_id
                        )
                        if url:
                            # Verify user ID matches
                            if self._verify_user_id(new_browser, account_id):
                                return (url, new_browser)
                            else:
                                logger.error(f"[{account_id}] User ID mismatch, aborting")
                                return ("USER_ID_MISMATCH", new_browser)
                except Exception as e:
                    logger.debug(f"[{account_id}] Selector {selector} failed: {e}")
                    continue

            logger.error(f"[{account_id}] Failed to navigate to any search result")
            return (None, browser)

        except Exception as e:
            logger.error(f"[{account_id}] Error navigating to profile: {e}")
            return (None, browser)

    def scrape(
        self, task: ScraperTask, browser: Any | None = None
    ) -> XiaohongshuFollowingResult:
        """Scrape following list from a Xiaohongshu account.

        Uses search-then-navigate approach (same as note scraper) since
        direct URL access to profiles is blocked by Xiaohongshu.

        Args:
            task: Task with account ID as task_key.
            browser: DrissionPage browser instance (optional if use_existing_browser=True).

        Returns:
            XiaohongshuFollowingResult with scraped following list.

        Raises:
            ScraperError: If scraping fails.
        """
        # Prepare browser - use existing browser connection or passed browser
        browser = self._prepare_browser(browser, task.task_key)

        account_id = task.task_key.strip()
        max_following = task.payload.get("max_following", self.max_following)

        logger.info(f"[{account_id}] Starting following scrape (max_following={max_following})")

        # Initialize per-run screenshots directory
        self._init_run_screenshots_dir(account_id)

        # Step 0: Restore cookies from previous session
        self._restore_cookies(browser)

        # Step 1: Navigate to Xiaohongshu homepage first
        logger.info(f"[{account_id}] Navigating to homepage")
        browser.get("https://www.xiaohongshu.com")
        time.sleep(3)

        # Step 2: Check login status on homepage
        if self._detect_login_required(browser):
            logger.info(f"[{account_id}] Login required")
            failed_ss = self._handle_login_flow(
                browser, account_id, account_id, trigger_popup=False,
            )
            if failed_ss is not None:
                return XiaohongshuFollowingResult(
                    success=False,
                    task_key=task.task_key,
                    task_type=self.task_type,
                    account_id=account_id,
                    login_required=True,
                    login_screenshot_path=failed_ss,
                    error="Login required to continue",
                )

        # Step 3: Search for the user (reuse parent class method)
        search_success = self._perform_search(browser, account_id)
        if not search_success:
            # Fallback to URL navigation
            search_url = self.build_url(task)
            logger.info(f"[{account_id}] Fallback to URL search: {search_url}")
            browser.get(search_url)
            time.sleep(5)
        self._save_page_screenshot(browser, account_id, "search")

        # Step 4: Navigate to user profile by clicking search result
        profile_url, browser = self._navigate_to_user_profile(browser, account_id)

        # Handle session expiry
        if profile_url == "LOGIN_REQUIRED":
            logger.warning(f"[{account_id}] Session expired, need to re-login")
            failed_ss = self._handle_login_flow(
                browser, account_id, account_id, trigger_popup=False,
            )
            if failed_ss is not None:
                return XiaohongshuFollowingResult(
                    success=False,
                    task_key=task.task_key,
                    task_type=self.task_type,
                    account_id=account_id,
                    login_required=True,
                    login_screenshot_path=failed_ss,
                    error="Session expired, login required",
                )
            # Retry navigation after re-login
            search_success = self._perform_search(browser, account_id)
            if not search_success:
                search_url = self.build_url(task)
                browser.get(search_url)
                time.sleep(5)
            profile_url, browser = self._navigate_to_user_profile(browser, account_id)

        if not profile_url or profile_url == "LOGIN_REQUIRED":
            logger.warning(f"[{account_id}] User profile not found")
            raise NoDataFoundError(task_key=task.task_key)

        if profile_url == "USER_ID_MISMATCH":
            logger.error(f"[{account_id}] User ID mismatch - search result does not match target user")
            raise NoDataFoundError(task_key=task.task_key)

        # Step 5: On profile page, get following count and screenshot
        logger.info(f"[{account_id}] Navigated to profile: {profile_url}")
        self._save_page_screenshot(browser, account_id, "profile")
        total_following = self._get_following_count(browser, account_id)

        # Step 6: Navigate to following list
        if not self._navigate_to_following_page(browser, account_id):
            logger.error(f"[{account_id}] Failed to navigate to following page")
            raise NoDataFoundError(task_key=task.task_key)

        self._save_page_screenshot(browser, account_id, "following_list")

        # Step 7: Extract following list with scrolling
        following_list = self._extract_following_list(browser, account_id, max_following)

        logger.info(
            f"[{account_id}] Complete: {len(following_list)} following users extracted"
        )

        return XiaohongshuFollowingResult(
            success=len(following_list) > 0,
            task_key=task.task_key,
            task_type=self.task_type,
            account_id=account_id,
            following=following_list,
            following_count=len(following_list),
            total_following=total_following,
        )

    def _get_following_count(self, browser: Any, account_id: str) -> int | None:
        """Get total following count from profile page.

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID being scraped.

        Returns:
            Total following count or None if not found.
        """
        try:
            html = browser.html

            # Try regex patterns first
            patterns = [
                r'关注[^\d]*(\d+(?:\.\d+)?[万亿]?)',
                r'"followingCount"\s*:\s*(\d+)',
                r'"following"\s*:\s*(\d+)',
            ]

            for pattern in patterns:
                match = re.search(pattern, html)
                if match:
                    count = self.parser.parse_count(match.group(1))
                    logger.info(f"[{account_id}] Total following: {count}")
                    return count

            # Try element selectors
            selectors = [
                "xpath://span[contains(text(), '关注')]/following-sibling::span",
                "xpath://div[contains(text(), '关注')]//span[@class='count']",
                "css:.user-info .following .count",
            ]

            for selector in selectors:
                try:
                    elem = browser.ele(selector, timeout=1)
                    if elem and elem.text.strip():
                        count = self.parser.parse_count(elem.text.strip())
                        logger.info(f"[{account_id}] Total following: {count}")
                        return count
                except Exception:
                    continue

            return None

        except Exception as e:
            logger.warning(f"[{account_id}] Failed to get following count: {e}")
            return None

    def _navigate_to_following_page(self, browser: Any, account_id: str) -> bool:
        """Navigate to the following list page.

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID being scraped (may be search term, not real user ID).

        Returns:
            True if navigation successful, False otherwise.
        """
        try:
            # Extract real user ID from current URL (may differ from search term)
            real_user_id = account_id
            current_url = browser.url
            if "/user/profile/" in current_url:
                match = re.search(r'/user/profile/([a-zA-Z0-9]+)', current_url)
                if match:
                    real_user_id = match.group(1)
                    logger.info(f"[{account_id}] Extracted real user ID: {real_user_id}")

            # Click on the following COUNT (e.g., "712 关注") to open following list
            # NOT the "关注" button which is for following the user
            try:
                # Look for the count + "关注" pattern (e.g., "712 关注")
                # The count is typically in a span before or after the "关注" text
                following_count_selectors = [
                    # Count element that's a sibling of "关注" text
                    "xpath://span[text()='关注']/preceding-sibling::span[1]",
                    "xpath://span[text()='关注']/..//span[contains(@class, 'count')]",
                    # Parent div containing both count and "关注"
                    "xpath://div[.//span[text()='关注'] and not(.//span[text()='粉丝'])]",
                    # Stats area with following count
                    "xpath://div[contains(@class, 'info')]//span[following-sibling::span[text()='关注']]",
                    "xpath://div[contains(@class, 'user-info')]//span[following-sibling::span[text()='关注']]",
                ]

                for selector in following_count_selectors:
                    try:
                        elem = browser.ele(selector, timeout=2)
                        if elem:
                            elem.click()
                            logger.info(f"[{account_id}] Clicked on following count ({selector})")
                            time.sleep(5)  # Wait longer for modal to load

                            # Save screenshot and HTML to see current state
                            self._save_page_screenshot(browser, account_id, "after_following_click")

                            # Save page HTML for debugging
                            html_path = os.path.join(
                                self.screenshots_dir,
                                f"{account_id}_after_following_click.html"
                            )
                            with open(html_path, "w", encoding="utf-8") as f:
                                f.write(browser.html)
                            logger.info(f"[{account_id}] Saved HTML to {html_path}")

                            # Check if following list modal is visible
                            if self._is_following_modal_visible(browser):
                                return True
                    except Exception as e:
                        logger.debug(f"[{account_id}] Selector {selector} failed: {e}")
                        continue

            except Exception as e:
                logger.warning(f"[{account_id}] Failed to click following count: {e}")

            # Method 2: Direct URL navigation with real user ID (fallback)
            following_url = f"https://www.xiaohongshu.com/user/profile/{real_user_id}/following"
            logger.info(f"[{account_id}] Trying direct URL: {following_url}")
            browser.get(following_url)
            time.sleep(3)

            if self._is_following_list_visible(browser):
                return True

            logger.warning(f"[{account_id}] Following list not visible")
            return False

        except Exception as e:
            logger.error(f"[{account_id}] Error navigating to following page: {e}")
            return False

    def _is_following_list_visible(self, browser: Any) -> bool:
        """Check if the following list is visible (legacy method).

        Args:
            browser: DrissionPage browser instance.

        Returns:
            True if following list is visible, False otherwise.
        """
        return self._is_following_modal_visible(browser)

    def _is_following_modal_visible(self, browser: Any) -> bool:
        """Check if the following list modal is visible.

        Looks for specific modal indicators, not just any user elements.

        Args:
            browser: DrissionPage browser instance.

        Returns:
            True if following modal is visible, False otherwise.
        """
        try:
            # Check URL first
            if "/following" in browser.url:
                return True

            # Look for modal-specific elements
            # The modal typically has a close button, scrollable list, etc.
            modal_selectors = [
                # Modal container
                "xpath://div[contains(@class, 'modal') and contains(@class, 'follow')]",
                "xpath://div[contains(@class, 'reds-popup')]",
                "xpath://div[contains(@class, 'reds-modal')]",
                # Modal with close button
                "xpath://div[.//svg[contains(@class, 'close')]]//div[contains(@class, 'user')]",
                # Modal with title "关注"
                "xpath://div[.//span[text()='关注列表'] or .//span[text()='TA关注的人']]",
                # Scrollable user list in modal
                "xpath://div[contains(@class, 'scroll')]//div[contains(@class, 'user-item')]",
                "xpath://div[contains(@class, 'list')]//div[contains(@class, 'user-item')]",
            ]

            for selector in modal_selectors:
                try:
                    elem = browser.ele(selector, timeout=2)
                    if elem:
                        logger.info(f"Following modal visible via: {selector}")
                        return True
                except Exception:
                    continue

            # Fallback: check if multiple user items exist that are NOT the profile header
            try:
                user_items = browser.eles("xpath://div[contains(@class, 'user-item') or contains(@class, 'follow-item')]")
                if user_items and len(user_items) > 1:
                    logger.info(f"Following modal visible: found {len(user_items)} user items")
                    return True
            except Exception:
                pass

            return False

        except Exception:
            return False

    def _extract_following_list(
        self,
        browser: Any,
        account_id: str,
        max_following: int,
    ) -> list[XiaohongshuFollowing]:
        """Extract following users from the list page.

        Args:
            browser: DrissionPage browser instance.
            account_id: Account ID being scraped (the follower).
            max_following: Maximum users to extract.

        Returns:
            List of XiaohongshuFollowing objects.
        """
        following_list: list[XiaohongshuFollowing] = []
        extracted_user_ids: set[str] = set()

        # User item selectors
        user_selectors = [
            "css:.follow-item",
            "css:.user-item",
            "css:.following-item",
            "xpath://div[contains(@class, 'follow') and contains(@class, 'item')]",
            "xpath://div[contains(@class, 'user') and contains(@class, 'item')]",
        ]

        max_scrolls = max_following // 10 + 20
        scroll_count = 0
        no_new_users_count = 0

        while len(following_list) < max_following and scroll_count < max_scrolls:
            # Find user elements
            user_elements = []
            for selector in user_selectors:
                try:
                    elements = browser.eles(selector)
                    if elements and len(elements) > len(user_elements):
                        user_elements = elements
                except Exception:
                    continue

            # Extract new users
            new_users_found = 0
            for elem in user_elements:
                if len(following_list) >= max_following:
                    break

                try:
                    user = self.parser.extract_single_following(elem, account_id)
                    if user and user.user_id not in extracted_user_ids:
                        extracted_user_ids.add(user.user_id)
                        following_list.append(user)
                        new_users_found += 1

                        # Save to database immediately
                        self.db.save_following(account_id, user)
                except Exception as e:
                    logger.debug(f"[{account_id}] Error extracting user: {e}")
                    continue

            if new_users_found == 0:
                no_new_users_count += 1
                if no_new_users_count >= 3:
                    logger.info(f"[{account_id}] No new users found after 3 scrolls, stopping")
                    break
            else:
                no_new_users_count = 0

            # Scroll for more users
            try:
                browser.scroll.to_bottom()
                time.sleep(2)
            except Exception as e:
                logger.debug(f"[{account_id}] Scroll error: {e}")
                break

            scroll_count += 1
            if scroll_count % 5 == 0:
                logger.info(
                    f"[{account_id}] Scroll {scroll_count}: extracted {len(following_list)} users"
                )

        return following_list


class XiaohongshuSearchAuthorScraper(XiaohongshuScraper):
    """Scraper to search keywords and extract authors from note cards.

    Searches for a keyword (e.g., aircraft registration number) and extracts
    author information from the note cards in search results without clicking
    into each note or user profile.

    Configuration options (in scraper config):
        max_results: Maximum note cards to scan per search (default: 20)
        screenshots_dir: Directory for screenshots
        All other options inherited from XiaohongshuScraper

    Task payload options:
        max_results: Override max_results for this task
    """

    task_type = "xiaohongshu_search_author"
    default_delay = (5.0, 10.0)
    requires_browser = True
    cloudflare_protected = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the search author scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)
        self.max_results = self.config.get("max_results", 20)

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate the task is appropriate for this scraper.

        Args:
            task: Task to validate.

        Returns:
            True if task is valid for this scraper.
        """
        return task.task_type == self.task_type and bool(task.task_key.strip())

    def build_url(self, task: ScraperTask) -> str:
        """Build search URL for the keyword.

        Args:
            task: Task with keyword as task_key.

        Returns:
            Search URL string.
        """
        keyword = task.task_key.strip()
        return f"https://www.xiaohongshu.com/search_result?keyword={keyword}"

    def scrape(
        self, task: ScraperTask, browser: Any | None = None
    ) -> XiaohongshuSearchAuthorResult:
        """Search keyword and extract authors from note cards.

        Args:
            task: Task with search keyword as task_key.
            browser: DrissionPage browser instance (optional if use_existing_browser=True).

        Returns:
            XiaohongshuSearchAuthorResult with extracted authors.

        Raises:
            ScraperError: If scraping fails.
        """
        # Prepare browser - use existing browser connection or passed browser
        browser = self._prepare_browser(browser, task.task_key)

        keyword = task.task_key.strip()  # Original registration for tracking
        search_keyword = f"飞机 {keyword}"  # Search with "飞机" prefix for better results
        max_results = task.payload.get("max_results", self.max_results)

        logger.info(f"[{keyword}] Starting search author scrape (search='{search_keyword}', max_results={max_results})")

        # Initialize per-run screenshots directory
        self._init_run_screenshots_dir(keyword)

        # Step 0: Restore cookies from previous session
        self._restore_cookies(browser)

        # Step 1: Navigate to homepage first
        logger.info(f"[{keyword}] Navigating to homepage")
        browser.get("https://www.xiaohongshu.com")
        time.sleep(3)

        # Step 2: Check login status
        if self._detect_login_required(browser):
            logger.info(f"[{keyword}] Login required")
            failed_ss = self._handle_login_flow(
                browser, keyword, keyword, trigger_popup=False,
            )
            if failed_ss is not None:
                return XiaohongshuSearchAuthorResult(
                    success=False,
                    task_key=task.task_key,
                    task_type=self.task_type,
                    keyword=keyword,
                    login_required=True,
                    login_screenshot_path=failed_ss,
                    error="Login required to continue",
                )

        # Step 3: Perform search with "飞机" prefix
        search_success = self._perform_search(browser, search_keyword)
        if not search_success:
            from urllib.parse import quote
            search_url = f"https://www.xiaohongshu.com/search_result?keyword={quote(search_keyword)}"
            logger.info(f"[{keyword}] Fallback to URL search: {search_url}")
            browser.get(search_url)
            time.sleep(5)

        self._save_page_screenshot(browser, keyword, "search_results")
        self._save_page_html(browser, keyword, "search_results")

        # Step 4: Check for login requirement on search results
        if self._detect_login_required(browser):
            logger.warning(f"[{keyword}] Login required on search results page")
            failed_ss = self._handle_login_flow(
                browser, keyword, keyword, trigger_popup=False,
            )
            if failed_ss is not None:
                return XiaohongshuSearchAuthorResult(
                    success=False,
                    task_key=task.task_key,
                    task_type=self.task_type,
                    keyword=keyword,
                    login_required=True,
                    login_screenshot_path=failed_ss,
                    error="Login required on search results",
                )
            # Retry search after login
            search_success = self._perform_search(browser, search_keyword)
            if not search_success:
                from urllib.parse import quote
                browser.get(f"https://www.xiaohongshu.com/search_result?keyword={quote(search_keyword)}")
                time.sleep(5)

        # Step 5: Extract authors from note cards
        authors, notes_scanned = self._extract_authors_from_search(
            browser, keyword, max_results
        )

        logger.info(
            f"[{keyword}] Complete: {len(authors)} unique authors from {notes_scanned} notes"
        )

        return XiaohongshuSearchAuthorResult(
            success=len(authors) > 0,
            task_key=task.task_key,
            task_type=self.task_type,
            keyword=keyword,
            authors=authors,
            authors_count=len(authors),
            notes_scanned=notes_scanned,
        )

    def _extract_initial_state(self, browser: Any, keyword: str) -> dict | None:
        """Extract __INITIAL_STATE__ from the page via JavaScript evaluation.

        XHS embeds structured search result data in window.__INITIAL_STATE__,
        which is more reliable than DOM parsing since it's the raw data before
        rendering.

        Args:
            browser: DrissionPage browser instance.
            keyword: Search keyword for logging.

        Returns:
            Parsed __INITIAL_STATE__ dict, or None if extraction fails.
        """
        js_code = """
        try {
            var state = window.__INITIAL_STATE__;
            if (!state) return null;
            // Handle Proxy objects — try JSON serialization
            return JSON.stringify(state);
        } catch(e) {
            // Fallback: try accessing search.feeds directly
            try {
                var feeds = window.__INITIAL_STATE__.search.feeds;
                // Handle .value / ._value wrapper
                if (feeds && feeds.value) feeds = feeds.value;
                if (feeds && feeds._value) feeds = feeds._value;
                if (!feeds) return null;
                return JSON.stringify({search: {feeds: feeds}});
            } catch(e2) {
                return null;
            }
        }
        """
        try:
            result = browser.run_js(js_code)
            if not result:
                logger.debug(f"[{keyword}] __INITIAL_STATE__ not available")
                return None

            import json
            state = json.loads(result)
            logger.info(f"[{keyword}] __INITIAL_STATE__ extracted successfully")
            return state

        except Exception as e:
            logger.debug(f"[{keyword}] Failed to extract __INITIAL_STATE__: {e}")
            return None

    def _extract_authors_from_search(
        self,
        browser: Any,
        keyword: str,
        max_results: int,
    ) -> tuple[list[XiaohongshuAuthor], int]:
        """Extract authors from search results.

        Uses __INITIAL_STATE__ as primary extraction method (structured JSON),
        falling back to DOM parsing if __INITIAL_STATE__ is not available.

        Args:
            browser: DrissionPage browser instance.
            keyword: Search keyword for logging.
            max_results: Maximum note cards to scan.

        Returns:
            Tuple of (list of unique authors, number of notes scanned).
        """
        # Primary: Try __INITIAL_STATE__ extraction (more reliable)
        initial_state = self._extract_initial_state(browser, keyword)
        if initial_state:
            authors = self.parser.extract_authors_from_initial_state(
                initial_state, keyword
            )
            if authors:
                # Save to database
                for author in authors[:max_results]:
                    self.db.save_author(author, discovered_from=keyword)
                authors = authors[:max_results]
                return authors, len(authors)
            logger.info(
                f"[{keyword}] __INITIAL_STATE__ had no authors, falling back to DOM"
            )

        # Fallback: DOM-based extraction
        return self._extract_authors_from_search_dom(browser, keyword, max_results)

    def _extract_authors_from_search_dom(
        self,
        browser: Any,
        keyword: str,
        max_results: int,
    ) -> tuple[list[XiaohongshuAuthor], int]:
        """Extract authors from note cards via DOM parsing (fallback method).

        Args:
            browser: DrissionPage browser instance.
            keyword: Search keyword for logging.
            max_results: Maximum note cards to scan.

        Returns:
            Tuple of (list of unique authors, number of notes scanned).
        """
        authors: list[XiaohongshuAuthor] = []
        extracted_user_ids: set[str] = set()
        notes_scanned = 0

        # Note card selectors
        note_card_selectors = [
            "css:section.note-item",
            "css:.note-item",
            "css:div[data-note-id]",
            "xpath://section[contains(@class, 'note-item')]",
            "xpath://div[contains(@class, 'note-item')]",
            "xpath://a[contains(@href, '/explore/')]",
        ]

        max_scrolls = max_results // 10 + 5
        scroll_count = 0
        no_new_notes_count = 0

        while notes_scanned < max_results and scroll_count < max_scrolls:
            # Find note card elements
            note_elements = []
            for selector in note_card_selectors:
                try:
                    elements = browser.eles(selector)
                    if elements and len(elements) > len(note_elements):
                        note_elements = elements
                except Exception:
                    continue

            if not note_elements:
                logger.warning(f"[{keyword}] No note cards found")
                break

            # Extract authors from note cards
            new_notes_found = 0
            for elem in note_elements:
                if notes_scanned >= max_results:
                    break

                try:
                    author = self.parser.extract_author_from_note_card(elem, keyword)
                    if author and author.user_id not in extracted_user_ids:
                        extracted_user_ids.add(author.user_id)
                        authors.append(author)

                        # Save author to database with registration as discovered_from
                        self.db.save_author(author, discovered_from=keyword)
                        logger.debug(
                            f"[{keyword}] Extracted author: {author.nickname} ({author.user_id})"
                        )

                    notes_scanned += 1
                    new_notes_found += 1

                except Exception as e:
                    logger.debug(f"[{keyword}] Error extracting from note card: {e}")
                    continue

            if new_notes_found == 0:
                no_new_notes_count += 1
                if no_new_notes_count >= 3:
                    logger.info(f"[{keyword}] No new notes found after 3 scrolls")
                    break
            else:
                no_new_notes_count = 0

            # Scroll to load more notes
            try:
                browser.scroll.to_bottom()
                time.sleep(2)
            except Exception as e:
                logger.debug(f"[{keyword}] Scroll error: {e}")
                break

            scroll_count += 1
            if scroll_count % 3 == 0:
                logger.info(
                    f"[{keyword}] Scroll {scroll_count}: scanned {notes_scanned} notes, "
                    f"found {len(authors)} unique authors"
                )

        return authors, notes_scanned


# =============================================================================
# CLI Interface
# =============================================================================


if __name__ == "__main__":
    from resilient_scraper.scrapers.xiaohongshu.db import (
        get_notes_without_images,
        get_notes_stats,
        reset_notes_for_rescrape,
    )

    import argparse

    parser = argparse.ArgumentParser(
        description="Xiaohongshu scraper utilities"
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="PostgreSQL database URL",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show notes statistics",
    )
    parser.add_argument(
        "--list-no-images",
        action="store_true",
        help="List notes without downloaded images",
    )
    parser.add_argument(
        "--reset-for-rescrape",
        action="store_true",
        help="Reset notes without images for re-scraping",
    )
    parser.add_argument(
        "--author-id",
        help="Filter by author ID",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Only show what would be done (default: True)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute the reset (overrides --dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Limit number of results shown (default: 20)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.stats:
        print("\n=== Xiaohongshu Notes Statistics ===\n")
        stats = get_notes_stats(args.database_url)
        for key, value in stats.items():
            print(f"  {key}: {value}")
        print()

    elif args.list_no_images:
        print("\n=== Notes Without Downloaded Images ===\n")
        notes = get_notes_without_images(args.database_url)
        print(f"Found {len(notes)} notes without images\n")
        for note in notes[:args.limit]:
            title = (note["title"] or "")[:50]
            urls_count = len(note["image_urls"]) if note["image_urls"] else 0
            print(f"  {note['note_id']} | {note['author_id']} | {title}... | {urls_count} URLs")
        if len(notes) > args.limit:
            print(f"\n  ... and {len(notes) - args.limit} more")

    elif args.reset_for_rescrape:
        dry_run = not args.execute
        print(f"\n=== Reset Notes for Re-scraping {'(DRY RUN)' if dry_run else ''} ===\n")
        count = reset_notes_for_rescrape(
            args.database_url,
            author_id=args.author_id,
            dry_run=dry_run,
        )
        if dry_run:
            print(f"\nWould reset {count} notes. Use --execute to actually reset.")
        else:
            print(f"\nReset {count} notes for re-scraping.")

    else:
        parser.print_help()
