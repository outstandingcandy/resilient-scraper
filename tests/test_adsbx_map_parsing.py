"""Unit tests for the ADS-B Exchange globe scraper's buffering and parsing.

The scraper hooks `window.processAircraft`, which tar1090 calls once per
aircraft *per feed poll*. Everything that makes the result correct happens after
the network is done with: keeping one entry per aircraft, deciding which are
military, and turning tar1090's abbreviated keys into the sink's column names.

None of that needs a browser, and none of it is exercised by a live test that
only checks the aircraft count is non-zero — a scraper that reports every feed
poll as a separate aircraft, or flags the wrong ones as military, still returns
plenty of rows.

The dedup is deliberately in two places. The hook does it in the page, because
the buffer crosses the CDP bridge as one JSON payload and buffering every poll
made that payload big enough to time out and disconnect the page; the Python
side keeps doing it too, so a page that stops deduping degrades to a slow
scrape rather than duplicate rows in the database. Both paths are covered here.
"""

import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from resilient_scraper.errors import ScraperError
from resilient_scraper.scrapers.aviation.adsbx_map.scraper import (
    _DRAIN_CHUNK,
    _FEED_FIELDS,
    _PROC_HOOK_JS,
    ADSBxMapScraper,
)

# An arbitrary fixed epoch standing in for tar1090's `now` global.
FEED_NOW = 1700000000.0


def _parse(rows: list[dict], feed_now: float | None = FEED_NOW) -> list:
    """Run rows through the dedup + parse step the way `scrape` does."""
    return ADSBxMapScraper({})._rows_to_aircraft(rows, feed_now)


def test_repeated_feed_ticks_collapse_to_one_aircraft() -> None:
    """The hook sees one row per tick per aircraft; the result holds one each."""
    rows = [
        {"hex": "3c4b26", "flight": "DLH123", "alt_baro": 30000},
        {"hex": "3c4b26", "flight": "DLH123", "alt_baro": 31000},
        {"hex": "3c4b26", "flight": "DLH123", "alt_baro": 32000},
        {"hex": "4ca7b5", "flight": "RYR456", "alt_baro": 12000},
    ]

    aircraft = _parse(rows)

    assert len(aircraft) == 2
    assert {a.hex for a in aircraft} == {"3c4b26", "4ca7b5"}


def test_the_latest_tick_wins() -> None:
    """Later rows are newer, so the surviving row must be the last one seen."""
    rows = [
        {"hex": "3c4b26", "alt_baro": 30000, "gs": 400.0},
        {"hex": "3c4b26", "alt_baro": 32000, "gs": 445.5},
    ]

    (aircraft,) = _parse(rows)

    assert aircraft.altitude_baro == 32000
    assert aircraft.ground_speed == 445.5


def test_hex_case_does_not_split_one_aircraft_into_two() -> None:
    """The dedup key is lowercased, so mixed-case feed rows still collapse."""
    aircraft = _parse([{"hex": "3C4B26"}, {"hex": "3c4b26"}, {"icao": "3c4b26"}])

    assert len(aircraft) == 1
    assert aircraft[0].hex == "3c4b26"


def test_rows_without_an_identifier_are_dropped() -> None:
    """A row with no hex cannot be keyed, stored, or matched to an airframe."""
    aircraft = _parse([{"hex": "3c4b26"}, {"flight": "DLH123"}, {"hex": ""}, "not-a-dict"])

    assert [a.hex for a in aircraft] == ["3c4b26"]


class TestMilitaryBit:
    """`dbFlags & 1`, which is the filter the whole production pipeline runs on.

    `config/scraper/fr24.yaml` sets `military_only: true`, so getting this bit
    wrong means the sink writes nothing at all or writes every civil airliner
    in the region as military.
    """

    def test_military_flag_is_set(self) -> None:
        (aircraft,) = _parse([{"hex": "43c6e1", "dbFlags": 1}])

        assert aircraft.mil is True
        assert aircraft.db_flags == 1

    def test_interesting_is_not_military(self) -> None:
        """dbFlags 2 is tar1090's "interesting" bit; bit 0 is clear."""
        (aircraft,) = _parse([{"hex": "3c4b26", "dbFlags": 2}])

        assert aircraft.mil is False

    def test_military_and_interesting_together_is_still_military(self) -> None:
        """A test for equality rather than the bit would miss this one."""
        (aircraft,) = _parse([{"hex": "43c6e1", "dbFlags": 3}])

        assert aircraft.mil is True

    def test_missing_flags_default_to_civil(self) -> None:
        (aircraft,) = _parse([{"hex": "3c4b26"}])

        assert aircraft.mil is False
        assert aircraft.db_flags is None


class TestGroundAltitude:
    """tar1090 sends `alt_baro: "ground"`, not a number, for taxiing aircraft."""

    def test_ground_string_becomes_a_flag_not_an_altitude(self) -> None:
        (aircraft,) = _parse([{"hex": "3c4b26", "alt_baro": "ground"}])

        assert aircraft.on_ground is True
        assert aircraft.altitude_baro is None

    def test_airborne_altitude_is_kept(self) -> None:
        (aircraft,) = _parse([{"hex": "3c4b26", "alt_baro": 30025}])

        assert aircraft.on_ground is False
        assert aircraft.altitude_baro == 30025


class TestTimestamp:
    """A position's age is `now - seen_pos`, both from the page.

    Feeding `now` straight through would date every aircraft in the batch
    identically and silently — a plausible value, and wrong by however long the
    aircraft has been out of coverage.
    """

    def test_position_time_is_the_feed_clock_minus_the_position_age(self) -> None:
        (aircraft,) = _parse([{"hex": "3c4b26", "seen_pos": 12.5}])

        assert aircraft.seen_pos == 12.5
        assert aircraft.timestamp == datetime.fromtimestamp(FEED_NOW - 12.5, tz=UTC)

    def test_timestamp_is_timezone_aware_utc(self) -> None:
        """A naive datetime here would be read back as local time downstream."""
        (aircraft,) = _parse([{"hex": "3c4b26", "seen_pos": 0.0}])

        assert aircraft.timestamp is not None
        assert aircraft.timestamp.tzinfo is not None
        assert aircraft.timestamp.utcoffset() == datetime.now(UTC).utcoffset()

    def test_a_fresh_position_has_no_age_subtracted(self) -> None:
        (aircraft,) = _parse([{"hex": "3c4b26"}])

        assert aircraft.timestamp == datetime.fromtimestamp(FEED_NOW, tz=UTC)

    def test_no_feed_clock_means_no_timestamp(self) -> None:
        """Better an absent time than one invented from the scraper's own clock."""
        (aircraft,) = _parse([{"hex": "3c4b26", "seen_pos": 12.5}], feed_now=None)

        assert aircraft.timestamp is None


class TestFieldMapping:
    """tar1090's abbreviated keys, which do not match the sink's column names."""

    def test_full_row_maps_every_field(self) -> None:
        rows = [
            {
                "hex": "43c6e1",
                "flight": "RRR7241 ",
                "r": "zz333 ",
                "t": "A400",
                "desc": "Airbus A400M Atlas",
                "lat": 51.2,
                "lon": -0.5,
                "alt_baro": 24000,
                "alt_geom": 24350,
                "gs": 288.4,
                "track": 92.1,
                "mag_heading": 90.0,
                "baro_rate": -640,
                "squawk": "7401",
                "category": "A4",
                "dbFlags": 1,
                "ownOp": "Royal Air Force",
                "messages": 1842,
                "rssi": -18.5,
            }
        ]

        (aircraft,) = _parse(rows)

        assert aircraft.aircraft_type == "A400"
        assert aircraft.type_description == "Airbus A400M Atlas"
        assert aircraft.latitude == 51.2
        assert aircraft.longitude == -0.5
        assert aircraft.altitude_geom == 24350
        assert aircraft.ground_speed == 288.4
        assert aircraft.track == 92.1
        assert aircraft.heading == 90.0
        assert aircraft.vertical_rate == -640
        assert aircraft.squawk == "7401"
        assert aircraft.category == "A4"
        assert aircraft.country == "Royal Air Force"
        assert aircraft.messages == 1842
        assert aircraft.rssi == -18.5

    def test_callsign_and_registration_are_normalised(self) -> None:
        """tar1090 pads callsigns to a fixed width; registrations arrive as `r`."""
        (aircraft,) = _parse([{"hex": "43c6e1", "flight": "RRR7241 ", "r": "zz333 "}])

        assert aircraft.flight == "RRR7241"
        assert aircraft.registration == "ZZ333"

    def test_blank_strings_become_none(self) -> None:
        """An all-spaces callsign is absent, not a callsign made of spaces."""
        (aircraft,) = _parse([{"hex": "43c6e1", "flight": "   ", "r": "", "t": ""}])

        assert aircraft.flight is None
        assert aircraft.registration is None
        assert aircraft.aircraft_type is None

    def test_true_heading_is_used_when_magnetic_is_absent(self) -> None:
        (aircraft,) = _parse([{"hex": "3c4b26", "true_heading": 271.5}])

        assert aircraft.heading == 271.5

    def test_geometric_rate_is_used_when_barometric_is_absent(self) -> None:
        (aircraft,) = _parse([{"hex": "3c4b26", "geom_rate": 1280}])

        assert aircraft.vertical_rate == 1280


class TestTaskValidation:
    """`validate_task` decides whether a queued region ever reaches the browser."""

    def test_coordinates_are_required(self) -> None:
        from resilient_scraper.models import ScraperTask

        scraper = ADSBxMapScraper({})

        assert (
            scraper.validate_task(
                ScraperTask(task_type="adsbx_map", task_key="europe_central", payload={})
            )
            is False
        )

    def test_out_of_range_coordinates_are_rejected(self) -> None:
        from resilient_scraper.models import ScraperTask

        scraper = ADSBxMapScraper({})
        task = ScraperTask(
            task_type="adsbx_map", task_key="bad", payload={"lat": 95.0, "lon": 10.0}
        )

        assert scraper.validate_task(task) is False

    def test_a_real_region_is_accepted(self) -> None:
        from resilient_scraper.models import ScraperTask

        scraper = ADSBxMapScraper({})
        task = ScraperTask(
            task_type="adsbx_map",
            task_key="europe_central",
            payload={"lat": 50.0, "lon": 10.0, "zoom": 5, "dbFlags": 1},
        )

        assert scraper.validate_task(task) is True

    def test_url_carries_the_region_and_the_flag_filter(self) -> None:
        from resilient_scraper.models import ScraperTask

        url = ADSBxMapScraper({}).build_url(
            ScraperTask(
                task_type="adsbx_map",
                task_key="europe_central",
                payload={"lat": 50.0, "lon": 10.0, "zoom": 5, "dbFlags": 1},
            )
        )

        assert url == "https://globe.adsbexchange.com/?dbFlags=1&lat=50.0&lon=10.0&zoom=5"


class TestFeedFieldList:
    """`_FEED_FIELDS` is the in-page allowlist, and it is silently lossy.

    The hook copies only these keys out of tar1090's aircraft objects, because
    the buffer crosses the CDP bridge as one JSON payload and the untrimmed
    objects made it 12 MB. The cost of that trim is a coupling with no runtime
    signal: a field read by the parser but missing from this list arrives as
    None on every aircraft, forever, without an error anywhere.
    """

    def test_every_field_the_parser_reads_is_copied_out_of_the_page(self) -> None:
        source = inspect.getsource(ADSBxMapScraper._parse_aircraft_dict)
        read_by_parser = set(re.findall(r'item\.get\("([^"]+)"\)', source))

        assert read_by_parser, "the parser's item.get() calls could not be located"
        missing = read_by_parser - set(_FEED_FIELDS)
        assert not missing, (
            f"{sorted(missing)} are read by _parse_aircraft_dict but never copied "
            f"out of the page, so they will always parse as None"
        )

    def test_the_allowlist_reaches_the_injected_script(self) -> None:
        """The list is interpolated into the JS; a broken splice loses every field."""
        assert json.dumps(list(_FEED_FIELDS)) in _PROC_HOOK_JS
        assert "__FEED_FIELDS__" not in _PROC_HOOK_JS

    def test_the_buffer_is_keyed_not_appended(self) -> None:
        """The dedup has to happen in the page, not just in Python.

        Buffering every feed poll is what produced the payload that timed out
        and disconnected the page, and the Python-side dedup runs far too late
        to prevent it.
        """
        assert "window.__adsbxRows = {}" in _PROC_HOOK_JS
        assert ".push(" not in _PROC_HOOK_JS


class _FakeBrowser:
    """A page holding `buffer` aircraft, served the way the real drain reads them.

    Understands the three scripts `_drain_rows` runs — the buffer size, an indexed
    slice of rows, and the feed clock — and can be told to fail one of them.

    `batches_as_json` mirrors what the page is actually asked for: the drain
    stringifies each batch, because CDP's returnByValue answered a 500-object
    array with an objectId instead. Set it False to model a bridge that
    serialises the array anyway — both forms have to work.
    """

    def __init__(
        self,
        buffer: dict[str, dict] | None = None,
        fail_on: str | None = None,
        feed_now: float | None = FEED_NOW,
        batches_as_json: bool = True,
    ) -> None:
        self.buffer = buffer if buffer is not None else {}
        self.fail_on = fail_on
        self.feed_now = feed_now
        self.batches_as_json = batches_as_json
        self.slices: list[tuple[int, int]] = []
        self.scripts: list[str] = []

    def run_js(self, script: str, *args: object, timeout: float | None = None) -> object:
        self.scripts.append(script)
        if self.fail_on and self.fail_on in script:
            raise RuntimeError("The connection to the page has been disconnected.")
        if ".map(" in script:
            start, stop = (int(n) for n in re.search(r"\.slice\((\d+), (\d+)\)", script).groups())
            self.slices.append((start, stop))
            batch = [self.buffer[k] for k in list(self.buffer)[start:stop]]
            return json.dumps(batch) if self.batches_as_json else batch
        if "Object.keys" in script:
            return len(self.buffer)
        if "__adsbxSeen" in script:
            return 4207
        if "typeof now" in script:
            return self.feed_now
        raise AssertionError(f"unexpected run_js: {script}")


class TestDrain:
    """Reading the buffer back off the page."""

    def test_buffered_aircraft_are_returned_with_the_feed_clock(self) -> None:
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26"}})

        rows, feed_ts = ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        assert rows == [{"hex": "3c4b26"}]
        assert feed_ts == FEED_NOW

    def test_an_empty_buffer_reads_as_no_aircraft(self) -> None:
        rows, _ = ADSBxMapScraper({})._drain_rows(_FakeBrowser({}), "pacific")

        assert rows == []

    def test_a_large_buffer_is_transferred_in_bounded_batches(self) -> None:
        """One payload for every aircraft in view is what stalled the CDP call.

        The batch size has to bound the transfer regardless of how busy the
        region is, so the number of rows per call must not grow with the buffer.
        """
        buffer = {f"{i:06x}": {"hex": f"{i:06x}"} for i in range(_DRAIN_CHUNK * 2 + 7)}
        browser = _FakeBrowser(buffer)

        rows, _ = ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        assert len(rows) == len(buffer), "every aircraft must survive the batching"
        assert [r["hex"] for r in rows] == list(buffer), "batches must not reorder or drop"
        assert len(browser.slices) == 3
        assert all(stop - start == _DRAIN_CHUNK for start, stop in browser.slices)

    def test_a_failed_transfer_raises_instead_of_reporting_an_empty_region(self) -> None:
        """This is the bug the live test caught: the payload killed the page.

        Swallowing it into `[]` made `scrape` return success with zero aircraft,
        which the task source records as "this region has no traffic" — so the
        region is never retried and the failure never appears anywhere.
        """
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26"}}, fail_on="Object.keys")

        with pytest.raises(ScraperError) as excinfo:
            ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        assert excinfo.value.retryable is True

    def test_a_transfer_that_dies_partway_also_raises(self) -> None:
        """Half a region is not a region; the partial rows must not be returned."""
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26"}}, fail_on=".map(")

        with pytest.raises(ScraperError):
            ADSBxMapScraper({})._drain_rows(browser, "europe_central")

    def test_a_missing_feed_clock_is_not_fatal(self) -> None:
        """Timestamps degrade to None; the positions themselves are still good."""
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26"}}, fail_on="typeof now")

        rows, feed_ts = ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        assert rows == [{"hex": "3c4b26"}]
        assert feed_ts is None

    def test_a_missing_diagnostic_counter_is_not_fatal(self) -> None:
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26"}}, fail_on="__adsbxSeen")

        rows, _ = ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        assert rows == [{"hex": "3c4b26"}]


class TestBatchTransferForm:
    """How a batch crosses the CDP bridge, which is where a full-fleet pass broke.

    Returning an array of 500 objects asks CDP to serialise it by value, which is
    best-effort: on a live probe it intermittently answered with an objectId
    instead ("js result parsing error ... 'description': 'Array(500)'"), and
    DrissionPage raised. That took out one region of six — an outright loss at
    exactly the volume `military_only: false` produces, and invisible until the
    counts were compared.

    A string is always returned by value, whatever its size.
    """

    def test_batches_are_stringified_in_the_page(self) -> None:
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26"}})

        ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        batch_scripts = [s for s in browser.scripts if ".map(" in s]
        assert batch_scripts
        assert all("JSON.stringify" in s for s in batch_scripts)

    def test_the_key_list_itself_never_crosses_the_bridge(self) -> None:
        """It is the largest array we would ask CDP to serialise, and unused —
        the slicing happens in the page, so only the count is needed."""
        browser = _FakeBrowser({f"{i:06x}": {"hex": f"{i:06x}"} for i in range(3)})

        ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        sizing = [s for s in browser.scripts if "Object.keys" in s and ".map(" not in s]
        assert sizing
        assert all(".length" in s for s in sizing)

    def test_a_json_batch_is_decoded(self) -> None:
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26", "lat": 51.2}})

        rows, _ = ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        assert rows == [{"hex": "3c4b26", "lat": 51.2}]

    def test_an_already_decoded_batch_still_works(self) -> None:
        """A bridge that serialises the array anyway must not be rejected."""
        browser = _FakeBrowser({"3c4b26": {"hex": "3c4b26"}}, batches_as_json=False)

        rows, _ = ADSBxMapScraper({})._drain_rows(browser, "europe_central")

        assert rows == [{"hex": "3c4b26"}]

    def test_a_large_buffer_survives_the_json_round_trip(self) -> None:
        """The probe's busiest window held 11,998 aircraft — 24 batches."""
        buffer = {f"{i:06x}": {"hex": f"{i:06x}"} for i in range(_DRAIN_CHUNK * 3 + 11)}

        rows, _ = ADSBxMapScraper({})._drain_rows(_FakeBrowser(buffer), "north_americas")

        assert [r["hex"] for r in rows] == list(buffer)

    def test_a_truncated_batch_raises_rather_than_losing_500_aircraft(self) -> None:
        class Truncating(_FakeBrowser):
            def run_js(self, script: str, *args: object, timeout: float | None = None) -> object:
                out = super().run_js(script, *args, timeout=timeout)
                return out[:-20] if ".map(" in script else out

        with pytest.raises(ScraperError) as excinfo:
            ADSBxMapScraper({})._drain_rows(
                Truncating({f"{i:06x}": {"hex": f"{i:06x}"} for i in range(5)}), "pacific"
            )

        assert excinfo.value.retryable is True

    def test_a_batch_of_the_wrong_type_is_dropped_not_crashed_on(self) -> None:
        class Weird(_FakeBrowser):
            def run_js(self, script: str, *args: object, timeout: float | None = None) -> object:
                out = super().run_js(script, *args, timeout=timeout)
                return 42 if ".map(" in script else out

        rows, _ = ADSBxMapScraper({})._drain_rows(
            Weird({"3c4b26": {"hex": "3c4b26"}}), "europe_central"
        )

        assert rows == []

    def test_non_dict_entries_inside_a_batch_are_filtered(self) -> None:
        """`_rows_to_aircraft` skips them too, but not before they are counted."""
        assert ADSBxMapScraper._decode_batch('[{"hex": "3c4b26"}, null, 7, "x"]') == [
            {"hex": "3c4b26"}
        ]


class TestHookInstall:
    """Waiting for the hook to report itself installed.

    `processAircraft` is defined by a late-loading bundle, so the wait is real.
    But a wait that gives up and continues drains an empty buffer and returns
    `success=True` with zero aircraft — the same silent failure as a dead
    transfer, and the task source records it as "this region has no traffic".
    Observed on roughly one page load in five during a probe run.
    """

    class _Page:
        def __init__(self, installed: bool) -> None:
            self.installed = installed
            self.calls = 0

        def run_js(self, script: str, *args: object, **kwargs: object) -> object:
            self.calls += 1
            return self.installed

    def test_an_installed_hook_returns_immediately(self) -> None:
        page = self._Page(installed=True)

        ADSBxMapScraper({})._wait_for_hook(page, "europe_central", timeout=5.0)

        assert page.calls == 1

    def test_a_hook_that_never_installs_raises_retryably(self) -> None:
        with pytest.raises(ScraperError) as excinfo:
            ADSBxMapScraper({})._wait_for_hook(self._Page(installed=False), "pacific", timeout=0.0)

        assert excinfo.value.retryable is True

    def test_an_unreadable_page_also_raises(self) -> None:
        """A disconnected page answers nothing, which is not "no traffic" either."""

        class Dead:
            def run_js(self, script: str, *args: object, **kwargs: object) -> object:
                raise RuntimeError("The connection to the page has been disconnected.")

        with pytest.raises(ScraperError):
            ADSBxMapScraper({})._wait_for_hook(Dead(), "pacific", timeout=0.0)


class TestDebugSnapshot:
    """The debug dump must not raise on the runs it exists to explain."""

    def test_a_dead_page_does_not_turn_the_diagnostic_into_the_error(self) -> None:
        class DeadPage:
            @property
            def html(self) -> str:
                raise RuntimeError("The connection to the page has been disconnected.")

        # Must not raise: reading the page was the whole reason we got here.
        ADSBxMapScraper({})._save_debug_files(DeadPage(), "europe_central")

    def test_the_page_is_written_when_it_is_readable(self) -> None:
        """A failing screenshot must not lose the HTML that was already saved."""

        class LivePage:
            html = "<html>the map</html>"

            def get_screenshot(self, path: str, full_page: bool = False) -> None:
                raise RuntimeError("no display")

        ADSBxMapScraper({})._save_debug_files(LivePage(), "written_by_test")

        written = Path("/tmp/adsbx_map_debug_written_by_test.html")
        try:
            assert written.read_text(encoding="utf-8") == "<html>the map</html>"
        finally:
            written.unlink(missing_ok=True)
