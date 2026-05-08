"""
eBay seller storefront scraper.

Given a seller slug (from `https://www.ebay.com/str/<slug>`), paginates
through the "All items" grid at 72 items per page, extracting listing
data from the embedded `$storenode_C` JSON payload and upserting each
page to `ebay_listings`.

eBay uses Radware StormCaster (`__uzdbm_3` / `__uzdbm_4`) for bot
protection — not Cloudflare — so this scraper uses its own
challenge-wait loop instead of the Cloudflare flow in the base class.
"""

import logging
import math
import os
import re
import time
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

from resilient_scraper import ResilientScraper
from resilient_scraper.errors import ScraperError
from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.ebay.db import EbayDB
from resilient_scraper.scrapers.ebay.models import EbayListing, EbayStoreResult
from resilient_scraper.scrapers.ebay.parser import EbayStoreParser

logger = logging.getLogger("scraper.ebay")


class EbayStoreScraper(ResilientScraper[EbayStoreResult]):
    """Scraper for eBay seller storefronts.

    Configuration options (in scraper config):
        max_pages: Maximum pages to scrape per seller (default: 50).
        skip_existing: Skip listing_ids already in DB (default: True).
        items_per_page: Items per page, capped at 72 by eBay (default: 72).
        screenshots_dir: Directory for screenshots (default: "data/ebay_screenshots").
        s3_upload: Whether to upload HTML to S3 (default: False).
        s3_bucket: S3 bucket name (required if s3_upload is True).
        s3_prefix: S3 key prefix (default: "data/ebay_raw").

    Task payload options:
        max_pages: Override scraper config's max_pages.
        skip_existing: Override scraper config's skip_existing.
    """

    task_type = "ebay_store"
    default_delay = (8.0, 15.0)
    requires_browser = True
    cloudflare_protected = False

    platform_display_name = "eBay"

    BASE_URL = "https://www.ebay.com"
    ITEMS_PER_PAGE = 72

    BOT_CHALLENGE_INDICATORS = [
        "verify you are human",
        "pardon our interruption",
        "unusual traffic",
        "security check",
        "please verify yourself",
        "radware",
    ]
    BOT_CHALLENGE_URL_FRAGMENTS = [
        "/splashui/captcha",
        "captcha.ebay",
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

        self.s3_prefix = self.config.get("s3_prefix", "data/ebay_raw")
        self.max_pages = int(self.config.get("max_pages", 50))
        self.skip_existing = bool(self.config.get("skip_existing", True))
        self.items_per_page = int(
            self.config.get("items_per_page", self.ITEMS_PER_PAGE)
        )

        self.db: EbayDB = EbayDB(self.db_engine)
        self.parser: EbayStoreParser = EbayStoreParser()
        self._existing_ids: set[str] = set()

    def setup(self) -> None:
        """Setup DB and parser."""
        super().setup()
        self.db = EbayDB(self.db_engine)
        self.db.ensure_tables_exist()
        self.parser = EbayStoreParser()

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that `task.task_key` is a legal eBay seller slug."""
        if not task.task_key:
            return False
        if not re.match(r"^[A-Za-z0-9._-]+$", task.task_key):
            logger.warning(f"Invalid eBay seller slug: {task.task_key}")
            return False
        return True

    def scrape(
        self, task: ScraperTask, browser: Any | None = None
    ) -> EbayStoreResult:
        """Scrape all pages of a seller's storefront."""
        browser = self._prepare_browser(browser, task.task_key)
        seller = task.task_key

        max_pages = int(task.payload.get("max_pages", self.max_pages))
        skip_existing = bool(task.payload.get("skip_existing", self.skip_existing))

        if skip_existing and self.db_engine:
            self._existing_ids = self.db.load_existing_listing_ids(seller)
        else:
            self._existing_ids = set()

        all_listings: list[EbayListing] = []
        s3_paths: list[str] = []
        records_updated = 0
        pages_scraped = 0
        total_items: int | None = None
        login_required = False
        login_screenshot_path: str | None = None
        consecutive_empty = 0

        # --- Page 1: load, solve challenge, read total count, compute page budget ---
        url = self._build_url(seller, 1)
        logger.info(f"[{seller}] Loading page 1: {url}")
        browser.get(url)
        time.sleep(8)

        if self._detect_bot_challenge(browser):
            login_screenshot_path = self._save_screenshot(
                browser, f"{seller}_challenge_page1"
            )
            login_required = True
            if self.wait_for_login_enabled:
                if not self._wait_for_bot_challenge(browser, seller):
                    raise ScraperError(
                        f"Bot challenge not resolved for {seller}",
                        task_key=seller,
                        retryable=True,
                    )
                login_required = False
            else:
                raise ScraperError(
                    f"Bot challenge blocked {seller}",
                    task_key=seller,
                    retryable=True,
                )

        html = browser.html or ""
        total_items = self.parser.extract_total_items(html)
        if total_items:
            total_pages = min(max_pages, math.ceil(total_items / self.items_per_page))
        else:
            total_pages = max_pages
        logger.info(
            f"[{seller}] total_items={total_items} total_pages={total_pages}"
        )

        html_path = self._upload_html_to_s3(html, f"{seller}/page1")
        if html_path:
            s3_paths.append(html_path)
        self._save_screenshot(browser, f"{seller}_page1")

        page1_listings = self._parse_listings(html, url, seller)
        logger.info(f"[{seller}] page 1: found {len(page1_listings)} listings")
        pages_scraped += 1
        new_p1 = self._filter_and_upsert(page1_listings, all_listings)
        records_updated += new_p1
        if not page1_listings:
            consecutive_empty += 1

        # --- Pages 2..N ---
        for page in range(2, total_pages + 1):
            self.wait_delay()
            url = self._build_url(seller, page)
            logger.info(f"[{seller}] Loading page {page}: {url}")
            try:
                browser.get(url)
                time.sleep(6)
            except Exception as e:
                logger.warning(f"[{seller}] page {page} load error: {e}")
                break

            if self._detect_bot_challenge(browser):
                logger.warning(f"[{seller}] challenge on page {page}")
                self._save_screenshot(browser, f"{seller}_challenge_page{page}")
                if self.wait_for_login_enabled:
                    if not self._wait_for_bot_challenge(browser, seller):
                        break
                else:
                    break

            html = browser.html or ""
            listings = self._parse_listings(html, url, seller)
            logger.info(
                f"[{seller}] page {page}: found {len(listings)} listings"
            )
            pages_scraped += 1

            if not listings:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    logger.info(
                        f"[{seller}] stopping: 2 consecutive empty pages"
                    )
                    break
                continue
            consecutive_empty = 0

            records_updated += self._filter_and_upsert(listings, all_listings)

        success = len(all_listings) > 0 or pages_scraped > 0
        logger.info(
            f"[{seller}] complete: {len(all_listings)} listings, "
            f"{pages_scraped} pages, {records_updated} records upserted"
        )

        return EbayStoreResult(
            success=success,
            task_key=seller,
            task_type=self.task_type,
            seller_username=seller,
            listings=all_listings,
            listings_count=len(all_listings),
            pages_scraped=pages_scraped,
            records_updated=records_updated,
            total_items=total_items,
            s3_paths=s3_paths,
            login_required=login_required,
            login_screenshot_path=login_screenshot_path,
        )

    # ------------------------------------------------------------------
    # listing extraction / persistence
    # ------------------------------------------------------------------

    def _parse_listings(
        self, html: str, source_url: str, seller: str
    ) -> list[EbayListing]:
        """Parse listings with JSON primary, DOM fallback."""
        listings = self.parser.extract_listings_from_json(html, source_url, seller)
        if listings:
            return listings
        logger.debug(f"[{seller}] JSON extraction empty, falling back to DOM")
        return self.parser.extract_listings_from_dom(html, source_url, seller)

    def _filter_and_upsert(
        self,
        page_listings: list[EbayListing],
        accumulator: list[EbayListing],
    ) -> int:
        """Filter out already-seen IDs, append to accumulator, upsert new ones."""
        new_ones: list[EbayListing] = []
        for listing in page_listings:
            if listing.listing_id in self._existing_ids:
                continue
            self._existing_ids.add(listing.listing_id)
            new_ones.append(listing)
            accumulator.append(listing)
        if not new_ones:
            return 0
        return self.db.upsert_listings(new_ones)

    # ------------------------------------------------------------------
    # bot challenge handling (Radware StormCaster)
    # ------------------------------------------------------------------

    def _detect_bot_challenge(self, browser: Any) -> bool:
        try:
            url = (getattr(browser, "url", "") or "").lower()
            if any(frag in url for frag in self.BOT_CHALLENGE_URL_FRAGMENTS):
                return True
            html = (browser.html or "").lower()
            title = (browser.title or "").lower()
            return any(
                ind in title or ind in html for ind in self.BOT_CHALLENGE_INDICATORS
            )
        except Exception:
            return False

    def _wait_for_bot_challenge(self, browser: Any, context_key: str) -> bool:
        """Wait for manual challenge resolution."""
        logger.info(
            f"[{context_key}] Bot challenge detected. "
            "Please complete the verification in the browser window..."
        )

        start_time = time.time()
        check_count = 0

        while time.time() - start_time < self.login_timeout:
            time.sleep(self.login_check_interval)
            check_count += 1

            if not self._detect_bot_challenge(browser):
                elapsed = time.time() - start_time
                logger.info(
                    f"[{context_key}] Bot challenge passed! "
                    f"(waited {elapsed:.1f}s)"
                )
                return True

            if check_count % 6 == 0:
                elapsed = time.time() - start_time
                logger.info(
                    f"[{context_key}] Waiting for bot challenge... "
                    f"({elapsed:.0f}s elapsed)"
                )

        logger.warning(
            f"[{context_key}] Bot challenge timeout after {self.login_timeout}s"
        )
        return False

    # ------------------------------------------------------------------
    # URL + persistence helpers
    # ------------------------------------------------------------------

    def _build_url(self, seller: str, page: int = 1) -> str:
        return (
            f"{self.BASE_URL}/str/{seller}"
            f"?_pgn={page}&_ipg={self.items_per_page}"
        )

    def _save_html_local(self, html: str, key_suffix: str) -> str | None:
        try:
            html_dir = f"{self.screenshots_dir}/html"
            os.makedirs(html_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_suffix = key_suffix.replace("/", "_")
            filename = f"{html_dir}/ebay_{safe_suffix}_{timestamp}.html"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(f"Saved HTML to {filename}")
            return filename
        except Exception as e:
            logger.warning(f"Failed to save HTML locally: {e}")
            return None

    def _upload_html_to_s3(self, html: str, key_suffix: str) -> str | None:
        local_path = self._save_html_local(html, key_suffix)

        if not self.s3_enabled or not self.s3_client:
            return local_path

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        s3_key = f"{self.s3_prefix}/{key_suffix}_{timestamp}.html"
        try:
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=s3_key,
                Body=html.encode("utf-8"),
                ContentType="text/html; charset=utf-8",
            )
            logger.info(f"Uploaded HTML to s3://{self.s3_bucket}/{s3_key}")
            return f"s3://{self.s3_bucket}/{s3_key}"
        except ClientError as e:
            logger.warning(f"Failed to upload HTML to S3: {e}")
            return local_path

    def _save_screenshot(self, browser: Any, suffix: str) -> str | None:
        try:
            os.makedirs(self.screenshots_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.screenshots_dir}/ebay_{suffix}_{timestamp}.png"
            if hasattr(browser, "get_screenshot"):
                browser.get_screenshot(path=filename)
            elif hasattr(browser, "save_screenshot"):
                browser.save_screenshot(filename)
            logger.info(f"Saved screenshot: {filename}")
            return filename
        except Exception as e:
            logger.warning(f"Failed to save screenshot: {e}")
            return None
