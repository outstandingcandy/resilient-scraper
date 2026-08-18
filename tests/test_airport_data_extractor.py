"""Regression tests for the airport-data.com detail-page extractor.

The site was rebuilt as a client-rendered app; the detail pages stayed
server-rendered but changed shape enough to break two things quietly:

  * A registration that was reused across airframes renders every airframe on
    one page, so extracting from the whole document produced a chimera — the
    current aircraft's identity with a scrapped predecessor's year, engine count
    and seating.
  * Value cells now share space with a "Search all <make> <model>" navigation
    link, which leaked into `model`.

Neither surfaced as a failure: both produce a populated record, just a wrong
one.
"""

from resilient_scraper.scrapers.aviation.airport_data.extractor import (
    AirportDataExtractor,
    aircraft_detail_url,
    first_record_html,
)

# Trimmed from the live page for N703PA, which carries a 1999 Cessna 208B and a
# 1959 Boeing 707 under the same registration. Each airframe is a
# `<div id="aircraftNNN">` card and the current one comes first.
_CESSNA_CARD = """
<div id="aircraft260550" class="card shadow-sm">
  <table class="table table-md"><tbody>
    <tr><td class="w-1/3 md:text-right align-top ">Manufacturer</td>
        <td><a href="https://airport-data.com/aircraft/manuf/Cessna">Cessna</a></td></tr>
    <tr><td class="w-1/3 md:text-right align-top ">Model</td>
        <td>208B                        <span class="ml-2">
              <a href="/aircraft/search/ac?field=model&amp;code=Cessna+208B" class="text-sm italic">
                Search all Cessna 208B                </a>
            </span>
        </td></tr>
    <tr><td>Year built</td><td>1999</td></tr>
    <tr><td>Number of Seats</td><td>12</td></tr>
    <tr><td>Number of Engines</td><td>1</td></tr>
    <tr><td>Mode S (ICAO24) Code</td><td>A960F9</td></tr>
    <tr><td>Current Status</td><td>Valid</td></tr>
  </tbody></table>
</div>
"""

_BOEING_CARD = """
<div id="aircraft691916" class="card shadow-sm">
  <table class="table table-md"><tbody>
    <tr><td>Manufacturer</td><td>Boeing</td></tr>
    <tr><td>Model</td><td>707-331</td></tr>
    <tr><td>Year built</td><td>1959</td></tr>
    <tr><td>Number of Seats</td><td>192</td></tr>
    <tr><td>Number of Engines</td><td>4</td></tr>
    <tr><td>Delivery Date</td><td>1959-12-30</td></tr>
  </tbody></table>
</div>
"""

TWO_RECORD_PAGE = f'<main><div class="container">{_CESSNA_CARD}{_BOEING_CARD}</div></main>'
ONE_RECORD_PAGE = f'<main><div class="container">{_BOEING_CARD}</div></main>'


def test_multi_record_page_yields_only_the_first_airframe() -> None:
    """Values must all come from the Cessna card, never mixed with the Boeing's."""
    data = AirportDataExtractor().extract(TWO_RECORD_PAGE, {"registration": "N703PA"})

    assert data["manufacturer"] == "Cessna"
    assert data["year_built"] == 1999
    assert data["engines"] == 1
    assert data["seats"] == 12
    assert data["mode_s_code"] == "A960F9"


def test_field_absent_from_first_record_is_not_borrowed_from_the_second() -> None:
    """The Cessna has no delivery date; the Boeing's must not fill the gap."""
    data = AirportDataExtractor().extract(TWO_RECORD_PAGE, {"registration": "N703PA"})

    assert data["delivery_date"] is None


def test_search_all_link_is_stripped_from_the_value() -> None:
    """`model` is the model, not the model plus the cell's navigation link."""
    data = AirportDataExtractor().extract(TWO_RECORD_PAGE, {"registration": "N703PA"})

    assert data["model"] == "208B"


def test_single_record_page_is_parsed_whole() -> None:
    """Narrowing must be a no-op when there is only one airframe."""
    assert first_record_html(ONE_RECORD_PAGE) == ONE_RECORD_PAGE

    data = AirportDataExtractor().extract(ONE_RECORD_PAGE, {"registration": "N703PA"})
    assert data["manufacturer"] == "Boeing"
    assert data["year_built"] == 1959
    assert data["delivery_date"] == "1959-12-30"


def test_unrecognised_markup_falls_back_to_the_whole_page() -> None:
    """If the card wrapper changes again, extract from everything rather than nothing."""
    html = "<table><tbody><tr><td>Manufacturer</td><td>Cessna</td></tr></tbody></table>"

    assert first_record_html(html) == html
    assert AirportDataExtractor().extract(html, {"registration": "N1"})["manufacturer"] == "Cessna"


def test_detail_url_omits_www_and_the_html_extension() -> None:
    """`www.` fails TLS verification and `.html` costs a redirect."""
    url = aircraft_detail_url("N703PA")

    assert url == "https://airport-data.com/aircraft/N703PA"
    assert "www." not in url
