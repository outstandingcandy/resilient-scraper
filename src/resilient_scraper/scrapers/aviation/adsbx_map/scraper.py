"""
ADS-B Exchange globe scraper.

The live feed at ``/re-api/`` is served as ``binCraft``-encoded, zstd-compressed
bytes and is only accepted when the site's native XHR issues the request
(direct fetches return 403). Rather than decode the binary format ourselves we
wrap ``window.processAircraft`` — the global tar1090 function that receives
every parsed aircraft record. Collected rows are deduplicated by hex and
filtered for the military bit (``dbFlags & 1``) before we hand them to the
flight-matrix sink.

DB persistence is delegated to the calling application via
``scraper.on_success`` (or the ``persist_*`` callbacks on richer scrapers).
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

from resilient_scraper.errors import PageLoadError, ScraperError
from resilient_scraper.models import ScraperTask
from resilient_scraper.scraper import ResilientScraper
from resilient_scraper.scrapers.aviation.adsbx_map.models import (
    ADSBxMapAircraftData,
    ADSBxMapResult,
)

logger = logging.getLogger("resilient_scraper.scrapers.adsbx_map")


# Wraps window.processAircraft. That function receives every aircraft record
# tar1090 decodes from the binCraft/zstd XHR, so hooking it is equivalent to
# decoding the feed ourselves — and keeps working if ADSBx changes transport.
# The wait() loop is needed because processAircraft is defined by a late-loading
# bundle; we retry every 50ms until it's installed.
_PROC_HOOK_JS = r"""
(function () {
    if (window.__adsbxProcHook) { return; }
    window.__adsbxProcHook = true;
    window.__adsbxRows = [];
    window.__adsbxProcInstalled = false;

    var install = function () {
        if (typeof window.processAircraft !== 'function') {
            setTimeout(install, 50);
            return;
        }
        var orig = window.processAircraft;
        window.processAircraft = function (ac, init, uat) {
            try {
                if (ac && !Array.isArray(ac) && typeof ac === 'object') {
                    var copy = {};
                    for (var k in ac) { copy[k] = ac[k]; }
                    window.__adsbxRows.push(copy);
                    if (window.__adsbxRows.length > 100000) {
                        window.__adsbxRows.shift();
                    }
                }
            } catch (_) { /* never let the hook break the page */ }
            return orig.apply(this, arguments);
        };
        window.__adsbxProcInstalled = true;
    };
    install();
})();
"""


class ADSBxMapScraper(ResilientScraper[ADSBxMapResult]):
    """Scraper for ADS-B Exchange globe view (military aircraft by default).

    Configuration keys (scraper config):
        wait_for_load: Seconds to wait after navigation for Cloudflare / first
            paint (default: 15).
        collect_duration: Seconds to let the hook accumulate feed updates
            before reading the buffer (default: 60). Military traffic is
            sparse — shorter windows will miss aircraft that only appear on
            the 2nd or 3rd feed poll.
        save_debug_html: Persist page HTML + screenshot when no aircraft land
            in the buffer (default: False).
        military_only: When True (default), only rows with ``dbFlags & 1`` are
            returned. Set False to capture every aircraft the page sees.

    Task payload:
        lat: Center latitude (required).
        lon: Center longitude (required).
        zoom: Map zoom level (default: 4).
        dbFlags: tar1090 URL filter — 1=military, 2=interesting, 4=PIA,
            8=LADD. Drives the client-side UI filter only; the feed is
            unfiltered, so we also apply ``military_only`` ourselves.
    """

    task_type = "adsbx_map"
    default_delay = (10.0, 20.0)
    requires_browser = True
    cloudflare_protected = True
    task_timeout = 300

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.wait_for_load = self.config.get("wait_for_load", 15)
        self.collect_duration = self.config.get("collect_duration", 60)
        self.save_debug_html = self.config.get("save_debug_html", False)
        self.military_only = self.config.get("military_only", True)

    def validate_task(self, task: ScraperTask) -> bool:
        payload = task.payload or {}
        lat = payload.get("lat")
        lon = payload.get("lon")
        if lat is None or lon is None:
            return False
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            return False
        return -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0

    def build_url(self, task: ScraperTask) -> str:
        payload = task.payload or {}
        lat = float(payload.get("lat", 0.0))
        lon = float(payload.get("lon", 0.0))
        zoom = int(payload.get("zoom", 4))
        db_flags = int(payload.get("dbFlags", 1))
        return (
            f"https://globe.adsbexchange.com/?dbFlags={db_flags}"
            f"&lat={lat}&lon={lon}&zoom={zoom}"
        )

    def scrape(
        self, task: ScraperTask, browser: Any | None = None
    ) -> ADSBxMapResult:
        if browser is None:
            raise ScraperError(
                "Browser required for ADS-B Exchange map scraper",
                task_key=task.task_key,
                retryable=False,
            )

        payload = task.payload or {}
        lat = float(payload.get("lat", 0.0))
        lon = float(payload.get("lon", 0.0))
        zoom = int(payload.get("zoom", 4))
        db_flags = int(payload.get("dbFlags", 1))

        span = 180 / (2**zoom)
        bounds = {
            "north": lat + span / 2,
            "south": lat - span / 2,
            "west": lon - span / 2,
            "east": lon + span / 2,
        }

        # Must run before the page loads so we wrap processAircraft the
        # instant tar1090 defines it. add_init_js persists across tab lifetime.
        self._install_proc_hook(browser, task.task_key)

        url = self.build_url(task)
        logger.info(f"[{task.task_key}] Visiting: {url}")
        try:
            browser.get(url)
        except Exception as e:
            msg = str(e).lower()
            if "timeout" in msg or "timed out" in msg:
                logger.error(f"[{task.task_key}] Page load timeout: {e}")
                raise PageLoadError(url, task_key=task.task_key) from e
            raise

        time.sleep(self.wait_for_load)

        self._dismiss_cookie_consent(browser, task.task_key)

        html = browser.html
        if "just a moment" in html.lower() or "checking your browser" in html.lower():
            logger.info(f"[{task.task_key}] Cloudflare detected, waiting...")
            if not self.handle_cloudflare(browser):
                raise ScraperError(
                    "Cloudflare challenge failed",
                    task_key=task.task_key,
                    retryable=True,
                )
            time.sleep(5)

        # Let the hook accumulate feed updates. Military traffic is sparse;
        # we need a few poll cycles to catch aircraft that weren't in the
        # first snapshot.
        self._wait_for_hook(browser, task.task_key)
        logger.info(
            f"[{task.task_key}] Collecting for {self.collect_duration}s..."
        )
        time.sleep(self.collect_duration)

        raw_rows, feed_ts = self._drain_rows(browser, task.task_key)
        aircraft = self._rows_to_aircraft(raw_rows, feed_ts)

        military_count = sum(1 for a in aircraft if a.mil)
        if self.military_only:
            aircraft = [a for a in aircraft if a.mil]

        if not aircraft and self.save_debug_html:
            self._save_debug_files(browser, task.task_key, browser.html)

        logger.info(
            f"[{task.task_key}] Extracted {len(aircraft)} aircraft "
            f"(seen_total={len(raw_rows)}, mil_in_feed={military_count}, "
            f"military_only={self.military_only}, dbFlags={db_flags})"
        )

        feed_dt = (
            datetime.fromtimestamp(feed_ts, tz=UTC)
            if feed_ts is not None
            else None
        )
        return ADSBxMapResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            center_lat=lat,
            center_lon=lon,
            zoom_level=zoom,
            bounds=bounds,
            aircraft=aircraft,
            aircraft_count=len(aircraft),
            military_count=military_count,
            feed_generated_at=feed_dt,
            scraped_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # Browser interaction helpers
    # ------------------------------------------------------------------

    def _install_proc_hook(self, browser: Any, task_key: str) -> None:
        """Wrap window.processAircraft on every future navigation."""
        try:
            if hasattr(browser, "add_init_js"):
                browser.add_init_js(_PROC_HOOK_JS)
            else:
                browser.run_js(_PROC_HOOK_JS)
        except Exception as e:
            logger.warning(
                f"[{task_key}] Failed to install processAircraft hook: {e}"
            )

    def _wait_for_hook(
        self, browser: Any, task_key: str, timeout: float = 30.0
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ok = browser.run_js(
                    "return window.__adsbxProcInstalled === true;"
                )
            except Exception:
                ok = False
            if ok:
                return
            time.sleep(1)
        logger.warning(
            f"[{task_key}] processAircraft hook never reported installed"
        )

    def _drain_rows(
        self, browser: Any, task_key: str
    ) -> tuple[list[dict[str, Any]], float | None]:
        """Pull accumulated rows and the feed's epoch timestamp off the page."""
        try:
            rows = browser.run_js("return window.__adsbxRows || [];") or []
        except Exception as e:
            logger.warning(f"[{task_key}] Failed to read __adsbxRows: {e}")
            rows = []
        try:
            feed_ts = browser.run_js(
                "return typeof now !== 'undefined' ? now : null;"
            )
        except Exception:
            feed_ts = None
        return rows, (float(feed_ts) if isinstance(feed_ts, (int, float)) else None)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _rows_to_aircraft(
        self, rows: list[dict[str, Any]], feed_now: float | None
    ) -> list[ADSBxMapAircraftData]:
        """Deduplicate rows by hex (keeping the most recent) and parse them."""
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            hex_id = row.get("hex") or row.get("icao")
            if not hex_id:
                continue
            # Later rows are newer; tar1090 pushes every feed tick so the
            # final entry per hex reflects the most recent known state.
            latest[str(hex_id).lower()] = row

        return [
            ac
            for row in latest.values()
            if (ac := self._parse_aircraft_dict(row, feed_now))
        ]

    def _parse_aircraft_dict(
        self, item: Any, now_epoch: float | None
    ) -> ADSBxMapAircraftData | None:
        if not isinstance(item, dict):
            return None
        hex_id = item.get("hex") or item.get("icao")
        if not hex_id:
            return None

        alt_baro_raw = item.get("alt_baro")
        on_ground = alt_baro_raw == "ground"
        alt_baro: int | None
        if isinstance(alt_baro_raw, (int, float)):
            alt_baro = int(alt_baro_raw)
        else:
            alt_baro = None

        db_flags = _as_int(item.get("dbFlags"))
        mil = bool(db_flags is not None and db_flags & 1)

        seen_pos = _as_float(item.get("seen_pos"))
        ts: datetime | None = None
        if now_epoch is not None:
            try:
                offset = seen_pos if seen_pos is not None else 0.0
                ts = datetime.fromtimestamp(float(now_epoch) - offset, tz=UTC)
            except (TypeError, ValueError, OSError):
                ts = None

        flight = item.get("flight")
        if isinstance(flight, str):
            flight = flight.strip() or None

        registration = item.get("r") or item.get("registration")
        if isinstance(registration, str):
            registration = registration.strip().upper() or None

        try:
            return ADSBxMapAircraftData(
                hex=str(hex_id).lower(),
                flight=flight if isinstance(flight, str) else None,
                registration=(
                    registration if isinstance(registration, str) else None
                ),
                aircraft_type=_as_str(item.get("t") or item.get("icaotype")),
                type_description=_as_str(
                    item.get("desc") or item.get("typeDescription")
                ),
                latitude=_as_float(item.get("lat")),
                longitude=_as_float(item.get("lon")),
                altitude_baro=alt_baro,
                altitude_geom=_as_int(item.get("alt_geom")),
                ground_speed=_as_float(item.get("gs")),
                track=_as_float(item.get("track")),
                heading=_as_float(
                    item.get("mag_heading") or item.get("true_heading")
                ),
                vertical_rate=_as_int(
                    item.get("baro_rate") or item.get("geom_rate")
                ),
                squawk=_as_str(item.get("squawk")),
                category=_as_str(item.get("category")),
                emergency=_as_str(item.get("emergency")),
                db_flags=db_flags,
                mil=mil,
                on_ground=on_ground,
                country=_as_str(item.get("country") or item.get("ownOp")),
                seen_pos=seen_pos,
                messages=_as_int(item.get("messages")),
                rssi=_as_float(item.get("rssi")),
                timestamp=ts,
            )
        except Exception as e:
            logger.debug(f"Failed to parse aircraft {hex_id}: {e}")
            return None

    # ------------------------------------------------------------------

    def _save_debug_files(self, browser: Any, task_key: str, html: str) -> None:
        try:
            debug_path = f"/tmp/adsbx_map_debug_{task_key}.html"
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(f"[{task_key}] Saved debug HTML to {debug_path}")
            try:
                screenshot_path = f"/tmp/adsbx_map_screenshot_{task_key}.png"
                browser.get_screenshot(path=screenshot_path, full_page=False)
                logger.info(f"[{task_key}] Saved screenshot to {screenshot_path}")
            except Exception as e:
                logger.warning(f"[{task_key}] Screenshot failed: {e}")
        except Exception as e:
            logger.warning(f"[{task_key}] Failed to save debug files: {e}")


def _as_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_int(v: Any) -> int | None:
    if v is None or v == "ground":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
