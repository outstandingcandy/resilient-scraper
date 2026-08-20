"""Regression tests for JetPhotos location extraction.

`airport_icao` came back empty for every photo ever scraped — 1.36 M rows, zero
filled — because the code took the ICAO from the `/airport/<slug>` href and
guarded it with `^[A-Z]{4}$`. The slug is a slug, so the guard never passed. The
code is right there in the link *text*: "Beijing Capital - ZBAA".

The awkward cases are real values from the production table: a closed airport
carries a suffix after the code, and plenty of locations have no ICAO at all and
must stay empty rather than be invented.
"""

from resilient_scraper.scrapers.aviation.jetphotos.extractor import JetPhotosExtractor


def a_page(airport_name: str, slug: str = "Beijing-Capital-ZBAA-China") -> str:
    """Photo Location block as JetPhotos renders it, trimmed to what is parsed."""
    return f"""
    <h2><span>Photo Location</span></h2>
    <div class="photo-details">
      <a href="/airport/{slug}">{airport_name}</a>
    </div>
    """


def location_of(html: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    JetPhotosExtractor()._extract_location(html, metadata)
    return metadata


class TestIcaoComesFromTheLinkText:
    def test_a_plain_airport_name_yields_its_icao(self) -> None:
        assert location_of(a_page("Beijing Capital - ZBAA"))["airport_icao"] == "ZBAA"

    def test_the_full_name_is_still_kept_as_the_location(self) -> None:
        metadata = location_of(a_page("Beijing Capital - ZBAA"))

        assert metadata["location"] == "Beijing Capital - ZBAA"
        assert metadata["airport_name"] == "Beijing Capital - ZBAA"

    def test_a_suffix_after_the_code_does_not_hide_it(self) -> None:
        """2,300 rows look like this."""
        assert location_of(a_page("Berlin Schoenefeld - EDDB (Closed)"))["airport_icao"] == "EDDB"

    def test_a_multi_word_name_does_not_confuse_the_split(self) -> None:
        name = "London - Heathrow - EGLL"

        assert location_of(a_page(name))["airport_icao"] == "EGLL"


class TestNamesWithNoIcao:
    def test_inflight_gets_no_code(self) -> None:
        assert "airport_icao" not in location_of(a_page("Inflight"))

    def test_a_code_with_a_digit_is_not_an_icao(self) -> None:
        """`Breighton Airfield - EG10` is a UK strip, not an ICAO aerodrome."""
        assert "airport_icao" not in location_of(a_page("Breighton Airfield - EG10"))

    def test_a_four_letter_word_in_the_name_is_not_mistaken_for_a_code(self) -> None:
        assert "airport_icao" not in location_of(a_page("Other Location - Some Museum"))

    def test_a_lowercase_tail_is_not_a_code(self) -> None:
        assert "airport_icao" not in location_of(a_page("Somewhere - home"))


class TestTheJsonLdPath:
    """JSON-LD sets `location` before `_extract_location` gets a chance to run, so
    a fix confined to that method would still have left the column empty on the
    pages that carry structured data — which is most of them."""

    def _page(self, location_name: str) -> str:
        return f"""
        <script type="application/ld+json">
        {{"@type": "Photograph", "dateCreated": "2024-03-15",
          "contentLocation": {{"@type": "Place", "name": "{location_name}"}}}}
        </script>
        {a_page(location_name)}
        """

    def test_the_icao_is_derived_from_the_structured_location(self) -> None:
        metadata = JetPhotosExtractor().extract(self._page("Beijing Capital - ZBAA"))

        assert metadata["location"] == "Beijing Capital - ZBAA"
        assert metadata["airport_icao"] == "ZBAA"

    def test_a_structured_location_with_no_code_stays_empty(self) -> None:
        assert JetPhotosExtractor().extract(self._page("Inflight"))["airport_icao"] is None


class TestFallbackLink:
    def test_a_page_without_the_heading_still_extracts(self) -> None:
        html = '<div><a href="/airport/ZBAA">Beijing Capital - ZBAA</a></div>'

        metadata = location_of(html)

        assert metadata["airport_icao"] == "ZBAA"
        assert metadata["location"] == "Beijing Capital - ZBAA"

    def test_nothing_is_set_when_there_is_no_airport_link(self) -> None:
        assert location_of("<div>no location here</div>") == {}
