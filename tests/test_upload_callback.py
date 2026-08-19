"""Tests for the injected ``upload_callback`` storage exit.

Everything this package stored went out through a boto3 client, so an
application deployed anywhere but AWS got nothing: the JetPhotos scraper's
downloaded images kept only their local copy, and the saved page HTML — the
whole point of which is to re-run extraction offline instead of re-scraping —
was never written at all. The bucket name came from an env var that does not
exist off AWS, so ``s3_enabled`` turned itself off and the uploads silently
became no-ops.

``upload_callback`` lets the caller supply the write. These tests pin the two
things that make it a fix rather than a second code path: the callback produces
*the same keys* boto3 would have, and it takes precedence over boto3 so a
non-AWS deployment never touches the SDK.
"""

import logging

import pytest
from resilient_scraper.models import ScraperTask
from resilient_scraper.scraper import ResilientScraper
from resilient_scraper.scrapers.aviation.jetphotos.scraper import JetPhotosScraper

PREFIX = "data/jetphotos_images"


class Recorder:
    """An application-side object store, standing in for S3/GCS/a directory."""

    def __init__(self, succeed: bool = True) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str | None] = {}
        self.cache_control: dict[str, str | None] = {}
        self.succeed = succeed

    def __call__(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> bool:
        if not self.succeed:
            return False
        self.objects[key] = data
        self.content_types[key] = content_type
        self.cache_control[key] = cache_control
        return True


class ExplodingCallback:
    """A callback that raises, as a caller's storage layer eventually will."""

    def __call__(self, *args: object, **kwargs: object) -> bool:
        raise RuntimeError("bucket does not exist")


class _Minimal(ResilientScraper):
    """The smallest concrete scraper: only the base storage code is under test."""

    task_type = "minimal"

    def scrape(self, task: ScraperTask, browser: object) -> object:  # pragma: no cover
        raise NotImplementedError

    def validate_task(self, task: ScraperTask) -> bool:  # pragma: no cover
        return True


def _jetphotos(**config: object) -> JetPhotosScraper:
    return JetPhotosScraper({"s3_prefix": PREFIX, **config})


class TestPageHtml:
    def test_the_html_reaches_the_callback_under_the_reextractor_key(self) -> None:
        # scripts/reextract_fields.py lists `data/jetphotos_images/html/` and
        # src/scraper/reextractor.py parses `<id>.html`, so this key is a
        # contract with the application, not an implementation detail.
        store = Recorder()
        scraper = _jetphotos(upload_callback=store)

        key = scraper._store_page_html("<html>page</html>", "9876543")

        assert key == f"{PREFIX}/html/9876543.html"
        assert store.objects[key] == b"<html>page</html>"
        assert store.content_types[key] == "text/html; charset=utf-8"

    def test_no_storage_at_all_means_no_key(self) -> None:
        # The `local` target with nothing configured: the scrape still succeeds,
        # it just has no stored page to point at.
        assert _jetphotos()._store_page_html("<html/>", "9876543") is None

    def test_a_photo_without_an_id_is_not_stored(self) -> None:
        store = Recorder()

        assert _jetphotos(upload_callback=store)._store_page_html("<html/>", "") is None
        assert store.objects == {}

    def test_a_failed_write_reports_no_key(self) -> None:
        # The row must not claim an html_s3_path that nothing can read back.
        scraper = _jetphotos(upload_callback=Recorder(succeed=False))

        assert scraper._store_page_html("<html/>", "9876543") is None


class TestImageUpload:
    @pytest.fixture
    def image(self, tmp_path: object) -> str:
        path = tmp_path / "B-1234_full_1772277610.jpg"  # type: ignore[operator]
        path.write_bytes(b"\xff\xd8jpeg-bytes")
        return str(path)

    def test_the_image_key_is_the_one_the_web_app_derives(self, image: str) -> None:
        # web_app.get_image_url() and src/media/thumbnails both assume
        # `<prefix>/<filename>`; a nested key would break every image URL.
        store = Recorder()
        scraper = _jetphotos(upload_callback=store)

        key = scraper._handle_upload(image)

        assert key == f"{PREFIX}/B-1234_full_1772277610.jpg"
        assert store.objects[key] == b"\xff\xd8jpeg-bytes"
        assert store.content_types[key] == "image/jpeg"

    def test_the_local_file_is_deleted_only_when_configured(self, image: str) -> None:
        import os

        scraper = _jetphotos(upload_callback=Recorder(), delete_local_after_upload=True)
        scraper._handle_upload(image)
        assert not os.path.exists(image)

    def test_a_failed_upload_keeps_the_local_file_and_the_key(self, image: str) -> None:
        import os

        scraper = _jetphotos(upload_callback=Recorder(succeed=False))

        # The key is still returned: the caller records where the image belongs
        # so scripts/upload_images_to_object_storage.py can backfill it.
        assert scraper._handle_upload(image) == f"{PREFIX}/B-1234_full_1772277610.jpg"
        assert os.path.exists(image)

    def test_a_missing_file_is_not_uploaded(self, tmp_path: object) -> None:
        store = Recorder()
        scraper = _jetphotos(upload_callback=store)

        scraper._handle_upload(str(tmp_path / "gone.jpg"))  # type: ignore[operator]

        assert store.objects == {}


class TestPrecedence:
    def test_the_callback_wins_over_the_s3_client(self, tmp_path: object) -> None:
        # An AWS deployment injects the callback too. boto3 must not also run,
        # or every object is written twice.
        class Boom:
            def put_object(self, **kwargs: object) -> None:
                raise AssertionError("boto3 was used despite an injected callback")

        store = Recorder()
        scraper = _jetphotos(upload_callback=store, s3_upload=True, s3_bucket="b")
        scraper.s3_client = Boom()

        assert scraper._store_page_html("<html/>", "1") == f"{PREFIX}/html/1.html"
        assert store.objects

    def test_setup_does_not_build_an_s3_client_when_a_callback_is_present(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
        scraper = _jetphotos(upload_callback=Recorder(), s3_upload=True, s3_bucket="bucket")

        scraper.setup()

        assert scraper.s3_client is None
        assert scraper.uploads_possible is True

    def test_uploads_are_impossible_with_neither(self) -> None:
        assert _jetphotos().uploads_possible is False

    def test_an_unresolved_bucket_still_leaves_the_callback_usable(self) -> None:
        # This is the gcp target exactly: S3_BUCKET_NAME is unset, so the
        # bucket arrives as the literal placeholder and S3 turns itself off.
        scraper = _jetphotos(
            upload_callback=Recorder(), s3_upload=True, s3_bucket="${S3_BUCKET_NAME}"
        )

        assert scraper.s3_enabled is False
        assert scraper.uploads_possible is True


class TestCallbackFailure:
    def test_an_exception_is_logged_and_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        # The callback belongs to the application, so its exception types are
        # not importable here. A stored copy is best-effort: losing it must not
        # fail a scrape that cost a page load and a Cloudflare wait.
        scraper = _jetphotos(upload_callback=ExplodingCallback())

        with caplog.at_level(logging.WARNING):
            assert scraper._store_page_html("<html/>", "9876543") is None

        assert "bucket does not exist" in caplog.text


class TestBaseClassKeys:
    def test_the_base_class_preserves_its_nested_keys(self, tmp_path: object) -> None:
        # Screenshots keep their subdirectory relative to screenshots_dir; the
        # shared helper must not flatten them into the JetPhotos shape.
        shots = tmp_path / "shots"  # type: ignore[operator]
        (shots / "run-1").mkdir(parents=True)
        path = shots / "run-1" / "01_load.png"
        path.write_bytes(b"png")
        store = Recorder()
        scraper = _Minimal(
            {"screenshots_dir": str(shots), "s3_prefix": "data/shots", "upload_callback": store}
        )

        key = scraper._handle_upload(str(path))

        assert key == "data/shots/run-1/01_load.png"
        assert store.content_types[key] == "image/png"
