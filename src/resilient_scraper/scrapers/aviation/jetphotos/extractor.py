"""
JetPhotos field extractor.

Extracts metadata from JetPhotos photo pages, including photographer info,
photo dates, location, camera details, and engagement metrics.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any

from resilient_scraper.extractors.base import BaseExtractor

logger = logging.getLogger("resilient_scraper.scrapers.jetphotos.extractor")


class JetPhotosExtractor(BaseExtractor):
    """Extractor for JetPhotos photo page metadata.

    Extracts the following fields:
    - jetphotos_id: Photo ID from URL
    - photographer: Photographer name
    - photo_date: Date photo was taken (ISO format)
    - upload_date: Date photo was uploaded (ISO format)
    - location: Location string
    - airport_icao: Airport ICAO code
    - airport_name: Airport name
    - notes: Photo notes/remarks
    - camera: Camera model
    - views: View count
    - likes: Like count
    - badges: Photo badges (e.g., "Photo of the Day")

    Context keys:
    - source_url: URL of the photo page (used to extract photo ID)
    """

    @property
    def version(self) -> str:
        """Extractor version."""
        return "1.0.0"

    def extract(self, html: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Extract metadata from a JetPhotos photo page.

        Args:
            html: HTML content of the photo page.
            context: Optional context with "source_url" key.

        Returns:
            Dictionary with extracted metadata fields.
        """
        context = context or {}
        source_url = context.get("source_url", "")

        metadata: dict[str, Any] = {
            "source_url": source_url,
            "jetphotos_id": None,
            "photographer": None,
            "photo_date": None,
            "upload_date": None,
            "location": None,
            "airport_icao": None,
            "airport_name": None,
            "notes": None,
            "camera": None,
            "views": None,
            "likes": None,
            "badges": None,
        }

        # Extract photo ID from URL
        if source_url:
            photo_id_match = re.search(r"/photo/(\d+)", source_url)
            if photo_id_match:
                metadata["jetphotos_id"] = photo_id_match.group(1)

        # Also try to find photo ID in HTML if not in URL
        if not metadata["jetphotos_id"]:
            photo_id_match = re.search(r'data-photo-id="(\d+)"', html)
            if photo_id_match:
                metadata["jetphotos_id"] = photo_id_match.group(1)

        # First try to extract from JSON-LD structured data (most reliable for some fields)
        self._extract_from_json_ld(html, metadata)

        # Extract fields using h3/h4 structure pattern
        self._extract_h3h4_fields(html, metadata)

        # Extract photographer
        if not metadata.get("photographer"):
            self._extract_photographer(html, metadata)

        # Extract location
        if not metadata.get("location"):
            self._extract_location(html, metadata)

        # Validate location - reject JSON fragments
        if metadata.get("location"):
            loc = metadata["location"]
            if any(x in loc for x in ["contentUrl", "datePublished", '{"@', "\\/"]):
                metadata["location"] = None

        # Extract notes/remarks
        if not metadata.get("notes"):
            self._extract_notes(html, metadata)

        return metadata

    def _extract_from_json_ld(
        self,
        html: str,
        metadata: dict[str, Any],
    ) -> None:
        """Extract metadata from JSON-LD structured data in the page.

        JetPhotos includes JSON-LD schema.org data with photo information.
        This is more reliable than regex-based extraction.

        Args:
            html: HTML content of the page.
            metadata: Dictionary to update with extracted data.
        """
        try:
            # Find JSON-LD script tag
            ld_match = re.search(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html,
                re.DOTALL | re.IGNORECASE,
            )
            if not ld_match:
                return

            ld_data = json.loads(ld_match.group(1))

            # Extract dateCreated (photo date)
            if ld_data.get("dateCreated") and not metadata.get("photo_date"):
                try:
                    date_str = ld_data["dateCreated"]
                    # Handle ISO format: 2024-03-15
                    if len(date_str) >= 10:
                        metadata["photo_date"] = date_str[:10]
                except (ValueError, TypeError):
                    pass

            # Extract datePublished (upload date)
            if ld_data.get("datePublished") and not metadata.get("upload_date"):
                try:
                    date_str = ld_data["datePublished"]
                    if len(date_str) >= 10:
                        metadata["upload_date"] = date_str[:10]
                except (ValueError, TypeError):
                    pass

            # Extract contentLocation (airport/location)
            content_location = ld_data.get("contentLocation")
            if content_location and not metadata.get("location"):
                if isinstance(content_location, dict):
                    loc_name = content_location.get("name", "")
                    if loc_name and "Undisclosed" not in loc_name:
                        metadata["location"] = loc_name
                        metadata["airport_name"] = loc_name
                elif isinstance(content_location, str):
                    if "Undisclosed" not in content_location:
                        metadata["location"] = content_location

            # Extract author/photographer
            author = ld_data.get("author")
            if author and not metadata.get("photographer"):
                if isinstance(author, dict):
                    metadata["photographer"] = author.get("name")
                elif isinstance(author, str):
                    metadata["photographer"] = author

            # Extract description as notes
            description = ld_data.get("description")
            if description and not metadata.get("notes"):
                # Only use if it's not the generic photo description
                if not description.startswith("Photo of"):
                    metadata["notes"] = description[:2000]

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"Failed to parse JSON-LD: {e}")

    def _extract_h3h4_value(self, html: str, field_name: str) -> str | None:
        """Extract value from <h3>Field Name</h3><h4>Value</h4> structure.

        Args:
            html: HTML content to search.
            field_name: The field name to look for in h3 tag.

        Returns:
            The extracted value or None if not found.
        """
        pattern = rf"{re.escape(field_name)}</h3>\s*<h4[^>]*>([^<]+)</h4>"
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else None

    def _parse_date_string(self, date_str: str) -> str | None:
        """Parse a date string into ISO format.

        Args:
            date_str: Date string in various formats (e.g., "Mar 15, 2024").

        Returns:
            ISO format date string (YYYY-MM-DD) or None if parsing fails.
        """
        formats = ["%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%b %d %Y"]
        for fmt in formats:
            try:
                parsed_date = datetime.strptime(date_str.strip(), fmt)
                return parsed_date.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _extract_h3h4_fields(self, html: str, metadata: dict[str, Any]) -> None:
        """Extract fields using h3/h4 HTML structure.

        Args:
            html: HTML content of the page.
            metadata: Dictionary to update with extracted data.
        """
        # Photo Date: <h3>Photo Date</h3><h4>Oct 31, 2025</h4>
        if not metadata.get("photo_date"):
            photo_date_str = self._extract_h3h4_value(html, "Photo Date")
            if photo_date_str:
                metadata["photo_date"] = self._parse_date_string(photo_date_str)

        # Upload Date: <h3>Uploaded</h3><h4>Nov 18, 2025</h4>
        if not metadata.get("upload_date"):
            upload_date_str = self._extract_h3h4_value(html, "Uploaded")
            if upload_date_str:
                metadata["upload_date"] = self._parse_date_string(upload_date_str)

        # Camera: <h3>Camera</h3><h4>Canon EOS R5</h4>
        camera_str = self._extract_h3h4_value(html, "Camera")
        if camera_str:
            metadata["camera"] = camera_str[:200]  # Limit to 200 chars

        # Views: <h3>Views</h3><h4>468</h4>
        views_str = self._extract_h3h4_value(html, "Views")
        if views_str:
            # Remove commas and parse as integer
            views_clean = views_str.replace(",", "").strip()
            try:
                metadata["views"] = int(views_clean)
            except ValueError:
                pass

        # Likes: <h3>Likes</h3><h4>10</h4>
        likes_str = self._extract_h3h4_value(html, "Likes")
        if likes_str:
            likes_clean = likes_str.replace(",", "").strip()
            try:
                metadata["likes"] = int(likes_clean)
            except ValueError:
                pass

        # Badges: <h3>Badges</h3><span>None</span> or "Photo of the Day", etc.
        badges_match = re.search(
            r"Badges</h3>\s*(?:<[^>]+>)*\s*([^<]+)",
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if badges_match:
            badges_text = badges_match.group(1).strip()
            if badges_text and badges_text.lower() != "none":
                metadata["badges"] = badges_text[:500]

    def _extract_photographer(self, html: str, metadata: dict[str, Any]) -> None:
        """Extract photographer name from HTML.

        Args:
            html: HTML content of the page.
            metadata: Dictionary to update with extracted data.
        """
        # Photographer: Extract from Photographer section with h6 tag
        # <h2><span>Photographer</span></h2>...<h6>Photographer Name</h6>
        photographer_match = re.search(
            r"Photographer</span></h2>.*?<h6[^>]*>([^<]+)</h6>",
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if photographer_match:
            metadata["photographer"] = photographer_match.group(1).strip()
        else:
            # Fallback: Look for photographer link
            photographer_link = re.search(
                r'href="/photographer/[^"]+">([^<]+)</a>',
                html,
            )
            if photographer_link:
                metadata["photographer"] = photographer_link.group(1).strip()

    def _extract_location(self, html: str, metadata: dict[str, Any]) -> None:
        """Extract location/airport from HTML.

        Args:
            html: HTML content of the page.
            metadata: Dictionary to update with extracted data.
        """
        # Location: Extract from Photo Location section
        # <h2><span>Photo Location</span></h2>...<a href="/airport/...">Airport Name</a>
        location_match = re.search(
            r'Photo Location</span></h2>.*?href="/airport/([^"]+)">([^<]+)</a>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if location_match:
            airport_code = location_match.group(1)
            airport_name = location_match.group(2).strip()
            metadata["location"] = airport_name
            metadata["airport_name"] = airport_name
            # Extract ICAO if it looks like one
            if re.match(r"^[A-Z]{4}$", airport_code):
                metadata["airport_icao"] = airport_code
        else:
            # Fallback: General airport link pattern
            airport_match = re.search(
                r'href="/airport/([A-Z]{3,4})">([^<]+)</a>',
                html,
            )
            if airport_match:
                metadata["airport_icao"] = airport_match.group(1)
                metadata["airport_name"] = airport_match.group(2).strip()
                metadata["location"] = metadata["airport_name"]

    def _extract_notes(self, html: str, metadata: dict[str, Any]) -> None:
        """Extract photo notes/remarks from HTML.

        Args:
            html: HTML content of the page.
            metadata: Dictionary to update with extracted data.
        """
        notes_patterns = [
            r'<div[^>]*class="[^"]*(?:photo-remark|remark)[^"]*"[^>]*>(.*?)</div>',
            r'<p[^>]*class="[^"]*remarks?[^"]*"[^>]*>(.*?)</p>',
            r"(?:Remark|Note|Comment)[s]?[:\s]*</(?:span|label|div|dt)>\s*<(?:span|dd|div)[^>]*>([^<]+)",
            r'data-remark="([^"]+)"',
            r'<(?:span|div)[^>]*class="[^"]*info-remark[^"]*"[^>]*>([^<]+)',
        ]

        for pattern in notes_patterns:
            notes_match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if notes_match:
                notes_text = notes_match.group(1).strip()
                notes_text = re.sub(r"<[^>]+>", " ", notes_text)
                notes_text = re.sub(r"&nbsp;", " ", notes_text)
                notes_text = re.sub(r"&amp;", "&", notes_text)
                notes_text = re.sub(r"&lt;", "<", notes_text)
                notes_text = re.sub(r"&gt;", ">", notes_text)
                notes_text = re.sub(r"\s+", " ", notes_text).strip()
                if notes_text and len(notes_text) > 3:
                    metadata["notes"] = notes_text[:2000]
                    break
