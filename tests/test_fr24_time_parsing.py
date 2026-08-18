"""Regression tests for the FR24 scrapers' UTC conversion.

Both FR24 scrapers converted times with `astimezone(UTC)` /
`fromtimestamp(tz=UTC)` without importing `UTC`, so every row raised NameError.
`_extract_flights` catches `Exception` per row and logs it at debug level, so
instead of failing loudly the scrapers reported "no flights found" for every
airport and every aircraft. Nothing linted this package, so F821 never ran.

These tests exercise each conversion branch so the names stay bound.
"""

from datetime import UTC, date, datetime

from resilient_scraper.scrapers.aviation.fr24_aircraft import FR24AircraftScraper
from resilient_scraper.scrapers.aviation.fr24_airport import FR24AirportArrivalsScraper

# The scrapers read FR24's displayed clock times as Asia/Shanghai (UTC+8) local
# time; 02:15 on Aug 18 Beijing is 18:15 UTC on Aug 17.
FLIGHT_DATE = date(2026, 8, 18)


def _airport_scraper() -> FR24AirportArrivalsScraper:
    return FR24AirportArrivalsScraper({"max_load_more_clicks": 0, "sync_to_database": False})


def test_ampm_clock_time_converts_to_utc() -> None:
    """FR24's primary format is 12-hour with AM/PM."""
    scheduled, _estimated, _actual = _airport_scraper()._extract_times(
        "<td>2:15 AM</td>", flight_date=FLIGHT_DATE
    )
    assert scheduled == datetime(2026, 8, 17, 18, 15, tzinfo=UTC)


def test_24_hour_clock_time_converts_to_utc() -> None:
    """Some rows carry a bare HH:MM with no AM/PM marker."""
    scheduled, _estimated, _actual = _airport_scraper()._extract_times(
        "<td>02:15</td>", flight_date=FLIGHT_DATE
    )
    assert scheduled == datetime(2026, 8, 17, 18, 15, tzinfo=UTC)


def test_unix_timestamp_is_read_as_utc() -> None:
    """`data-timestamp` is already an epoch, so it needs no timezone guess."""
    scheduled, _estimated, _actual = _airport_scraper()._extract_times(
        '<td data-timestamp="1755454500"></td>', flight_date=FLIGHT_DATE
    )
    assert scheduled == datetime.fromtimestamp(1755454500, tz=UTC)


def test_iso_timestamp_converts_to_utc() -> None:
    """ISO strings are treated as local (Beijing) time, same as the clock times."""
    scheduled, _estimated, _actual = _airport_scraper()._extract_times(
        "<td>2026-08-18 02:15</td>", flight_date=FLIGHT_DATE
    )
    assert scheduled == datetime(2026, 8, 17, 18, 15, tzinfo=UTC)


def test_millisecond_timestamp_is_scaled_down() -> None:
    """13-digit values are milliseconds and must not land in the year 57000."""
    scheduled, _estimated, _actual = _airport_scraper()._extract_times(
        '<td data-timestamp="1755454500000"></td>', flight_date=FLIGHT_DATE
    )
    assert scheduled == datetime.fromtimestamp(1755454500, tz=UTC)


def test_aircraft_page_row_timestamps_parse() -> None:
    """The per-aircraft page carries epochs rather than clock times."""
    scraper = FR24AircraftScraper({"sync_to_database": False})
    row = (
        '<tr><td><a href="/data/flights/lh446">LH446</a></td>'
        '<td data-timestamp="1755454500"></td>'
        '<td data-timestamp="1755490800"></td></tr>'
    )
    flight = scraper._parse_flight_row(row, "D-AIXA")
    assert flight is not None
    assert flight.scheduled_time == datetime.fromtimestamp(1755454500, tz=UTC)
    assert flight.actual_time == datetime.fromtimestamp(1755490800, tz=UTC)
