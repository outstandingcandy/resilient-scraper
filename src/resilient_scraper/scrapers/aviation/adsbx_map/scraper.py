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

import json
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

# Aircraft per `run_js` when reading the buffer back. Even deduplicated and
# trimmed, one payload for every aircraft in view was large enough to stall the
# CDP call and disconnect the page on the busiest regions; a bounded batch size
# keeps the transfer independent of how much traffic the region has. A measured
# full-fleet pass peaked at 11,998 aircraft in one window, so this is 24 batches
# rather than the 1 the military-only feed needed.
_DRAIN_CHUNK = 500
_DRAIN_TIMEOUT = 60.0


# The only fields copied out of tar1090's aircraft objects. Those objects carry
# far more than we store (renderer state, nav modes, per-source signal stats),
# and the buffer has to cross the CDP bridge in one JSON payload — see
# _PROC_HOOK_JS. A field read by _parse_aircraft_dict must be listed here or it
# will silently arrive as None.
_FEED_FIELDS = (
    "hex",
    "icao",
    "flight",
    "r",
    "registration",
    "t",
    "icaotype",
    "desc",
    "typeDescription",
    "lat",
    "lon",
    "alt_baro",
    "alt_geom",
    "gs",
    "track",
    "mag_heading",
    "true_heading",
    "baro_rate",
    "geom_rate",
    "squawk",
    "category",
    "emergency",
    "dbFlags",
    "country",
    "ownOp",
    "seen_pos",
    "messages",
    "rssi",
)

# Wraps window.processAircraft. That function receives every aircraft record
# tar1090 decodes from the binCraft/zstd XHR, so hooking it is equivalent to
# decoding the feed ourselves — and keeps working if ADSBx changes transport.
# The wait() loop is needed because processAircraft is defined by a late-loading
# bundle; we retry every 50ms until it's installed.
#
# The buffer is a map keyed by hex, not a list of every call. tar1090 invokes
# processAircraft once per aircraft *per feed poll*, so a 60-second window over
# busy airspace produced ~17,000 entries for ~4,400 aircraft and a 12 MB
# `run_js` payload that took 30s and intermittently disconnected the page —
# which _drain_rows then reported as an empty region. Keeping only the newest
# entry per aircraft and only the fields we parse makes the payload ~1.4 MB and,
# more importantly, bounds it by the number of aircraft in view rather than by
# how long we collect.
_PROC_HOOK_JS = r"""
(function () {
    if (window.__adsbxProcHook) { return; }
    window.__adsbxProcHook = true;
    window.__adsbxRows = {};
    window.__adsbxSeen = 0;
    window.__adsbxProcInstalled = false;
    var KEYS = __FEED_FIELDS__;

    var install = function () {
        if (typeof window.processAircraft !== 'function') {
            setTimeout(install, 50);
            return;
        }
        var orig = window.processAircraft;
        window.processAircraft = function (ac, init, uat) {
            try {
                if (ac && !Array.isArray(ac) && typeof ac === 'object') {
                    var hex = ac.hex || ac.icao;
                    if (hex) {
                        var copy = {};
                        for (var i = 0; i < KEYS.length; i++) {
                            var k = KEYS[i];
                            if (ac[k] !== undefined) { copy[k] = ac[k]; }
                        }
                        // Last poll wins, matching _rows_to_aircraft.
                        window.__adsbxRows[String(hex).toLowerCase()] = copy;
                        window.__adsbxSeen++;
                    }
                }
            } catch (_) { /* never let the hook break the page */ }
            return orig.apply(this, arguments);
        };
        window.__adsbxProcInstalled = true;
    };
    install();
})();
""".replace("__FEED_FIELDS__", json.dumps(list(_FEED_FIELDS)))


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
        return f"https://globe.adsbexchange.com/?dbFlags={db_flags}&lat={lat}&lon={lon}&zoom={zoom}"

    def scrape(self, task: ScraperTask, browser: Any | None = None) -> ADSBxMapResult:
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
        logger.info(f"[{task.task_key}] Collecting for {self.collect_duration}s...")
        time.sleep(self.collect_duration)

        raw_rows, feed_ts = self._drain_rows(browser, task.task_key)
        aircraft = self._rows_to_aircraft(raw_rows, feed_ts)

        military_count = sum(1 for a in aircraft if a.mil)
        if self.military_only:
            aircraft = [a for a in aircraft if a.mil]

        if not aircraft and self.save_debug_html:
            self._save_debug_files(browser, task.task_key)

        logger.info(
            f"[{task.task_key}] Extracted {len(aircraft)} aircraft "
            f"(seen_total={len(raw_rows)}, mil_in_feed={military_count}, "
            f"military_only={self.military_only}, dbFlags={db_flags})"
        )

        feed_dt = datetime.fromtimestamp(feed_ts, tz=UTC) if feed_ts is not None else None
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
            logger.warning(f"[{task_key}] Failed to install processAircraft hook: {e}")

    def _wait_for_hook(self, browser: Any, task_key: str, timeout: float = 30.0) -> None:
        """Block until the injected hook reports itself installed.

        Args:
            browser: The browser whose page should have run ``_PROC_HOOK_JS``.
            task_key: Region name, for logging.
            timeout: Seconds to wait for the late-loading tar1090 bundle.

        Raises:
            ScraperError: Retryable, when the hook never installs. Continuing
                anyway drains an empty buffer and returns ``success=True`` with
                zero aircraft, which the task source records as "this region has
                no traffic" — so the region is never retried and the failure
                appears nowhere. Observed on roughly one page load in five.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ok = browser.run_js("return window.__adsbxProcInstalled === true;")
            except Exception:
                ok = False
            if ok:
                return
            time.sleep(1)
        if self.save_debug_html:
            # The page as it was when the hook gave up is the only evidence of
            # why — a Cloudflare interstitial and a slow bundle look identical
            # from here.
            self._save_debug_files(browser, task_key)
        raise ScraperError(
            f"processAircraft hook never installed within {timeout:.0f}s",
            task_key=task_key,
            retryable=True,
        )

    def _drain_rows(self, browser: Any, task_key: str) -> tuple[list[dict[str, Any]], float | None]:
        """Pull the buffered aircraft and the feed's epoch timestamp off the page.

        Args:
            browser: The browser whose page holds the hook's buffer.
            task_key: Region name, for logging.

        Returns:
            The buffered rows (one per aircraft) and the feed clock, or None when
            the page does not expose it.

        Raises:
            ScraperError: Retryable, when the buffer cannot be read. An empty
                list here is indistinguishable from a region with no traffic, so
                a failed transfer must not be reported as one — the region would
                be recorded as empty and not retried.
        """
        try:
            # The count, not the keys: the slicing happens in the page, so the
            # key list itself never needs to cross the bridge — and at full-fleet
            # volume it is the single largest array we would ask CDP to serialise.
            total = browser.run_js("return Object.keys(window.__adsbxRows || {}).length;") or 0
            total = int(total)
            rows: list[dict[str, Any]] = []
            for start in range(0, total, _DRAIN_CHUNK):
                # Slice in the page and transfer a bounded number of rows at a
                # time. Key order is insertion order and the hook only ever
                # appends new aircraft, so indices stay valid while we read;
                # aircraft that arrive mid-drain are simply left for next time.
                #
                # Stringified in the page and decoded here rather than returned as
                # an array of objects: CDP's returnByValue is best-effort, and on a
                # 500-object array it intermittently answers with an objectId
                # instead, which DrissionPage cannot parse ("js result parsing
                # error ... 'description': 'Array(500)'"). That killed one region
                # out of six on a full-fleet pass. A string is always returned by
                # value, whatever its size.
                batch = browser.run_js(
                    "return JSON.stringify(Object.keys(window.__adsbxRows || {})"
                    f".slice({start}, {start + _DRAIN_CHUNK})"
                    ".map(function (k) { return window.__adsbxRows[k]; }));",
                    timeout=_DRAIN_TIMEOUT,
                )
                rows.extend(self._decode_batch(batch))
        except Exception as e:
            raise ScraperError(
                # The class name matters: DrissionPage's parsing error carries an
                # empty message, so "{e}" alone logs a bare colon.
                f"Failed to read the aircraft buffer off the page: {type(e).__name__}: {e}",
                task_key=task_key,
                retryable=True,
            ) from e
        try:
            seen = browser.run_js("return window.__adsbxSeen || 0;")
            logger.info(
                f"[{task_key}] Drained {len(rows)}/{total} aircraft from {seen} feed "
                f"updates in {1 + total // _DRAIN_CHUNK} batches"
            )
        except Exception:  # noqa: S110 - a diagnostic counter, not worth failing on
            pass
        try:
            feed_ts = browser.run_js("return typeof now !== 'undefined' ? now : null;")
        except Exception:
            feed_ts = None
        return rows, (float(feed_ts) if isinstance(feed_ts, (int, float)) else None)

    @staticmethod
    def _decode_batch(batch: Any) -> list[dict[str, Any]]:
        """Decode one drained batch, whichever form the bridge returned it in.

        The page stringifies each batch, but a browser build that serialises the
        array by value anyway would return it as a list — accept both rather than
        depend on which side of that behaviour we get.

        Args:
            batch: The raw ``run_js`` return value: a JSON string, an already
                decoded list, or ``None`` when the page had nothing to give.

        Returns:
            The batch's aircraft dicts, empty if it was blank or malformed.

        Raises:
            ScraperError: When the batch is a string that is not valid JSON. A
                truncated transfer would otherwise silently drop 500 aircraft.
        """
        if not batch:
            return []
        if isinstance(batch, list):
            return [r for r in batch if isinstance(r, dict)]
        if isinstance(batch, str):
            try:
                decoded = json.loads(batch)
            except json.JSONDecodeError as e:
                raise ScraperError(f"Drained batch was not valid JSON: {e}", retryable=True) from e
            if isinstance(decoded, list):
                return [r for r in decoded if isinstance(r, dict)]
        logger.warning(f"Ignoring drained batch of unexpected type {type(batch).__name__}")
        return []

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

        return [ac for row in latest.values() if (ac := self._parse_aircraft_dict(row, feed_now))]

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
                registration=(registration if isinstance(registration, str) else None),
                aircraft_type=_as_str(item.get("t") or item.get("icaotype")),
                type_description=_as_str(item.get("desc") or item.get("typeDescription")),
                latitude=_as_float(item.get("lat")),
                longitude=_as_float(item.get("lon")),
                altitude_baro=alt_baro,
                altitude_geom=_as_int(item.get("alt_geom")),
                ground_speed=_as_float(item.get("gs")),
                track=_as_float(item.get("track")),
                heading=_as_float(item.get("mag_heading") or item.get("true_heading")),
                vertical_rate=_as_int(item.get("baro_rate") or item.get("geom_rate")),
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

    def _save_debug_files(self, browser: Any, task_key: str) -> None:
        """Persist the page for diagnosis, reading it inside the guard.

        The HTML is fetched here rather than passed in: an empty buffer and a
        dead page look the same from outside, and reading `browser.html` at the
        call site made this diagnostic raise on exactly the runs it exists to
        explain.
        """
        try:
            debug_path = f"/tmp/adsbx_map_debug_{task_key}.html"
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(browser.html)
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
