"""Aviation-domain scrapers (FR24, JetPhotos, airport-data.com).

Concrete DB persistence is delegated to the calling application via the
``scraper.on_success`` / ``scraper.on_failure`` hooks. These scrapers return
structured Pydantic results and do not touch any application-owned tables.
"""
