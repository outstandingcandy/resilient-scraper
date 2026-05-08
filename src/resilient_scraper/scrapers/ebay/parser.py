"""
eBay store page parsing logic.

Pure parsing module with no browser, database, or configuration dependencies.
Accepts raw HTML and returns parsed data structures.

Primary extraction uses the embedded `$storenode_C` JSON payload (clean,
structured). Falls back to regex DOM scraping only if the payload is missing.
"""

import json
import logging
import re
from typing import Any

from resilient_scraper.scrapers.ebay.models import EbayListing

logger = logging.getLogger("scraper.ebay.parser")


# Matches the first `$storenode_C=(window.$storenode_C||[]).concat({...})`
# script body on the page. Balanced-paren matching is done manually below
# because the JSON contains nested braces; the regex only locates the start.
_STORENODE_START = re.compile(
    r"\$storenode_C\s*=\s*\(window\.\$storenode_C\s*\|\|\s*\[\]\)\.concat\("
)


class EbayStoreParser:
    """Parse eBay seller storefront pages."""

    def extract_total_items(self, html: str) -> int | None:
        """Return the seller's total item count from `Search all N items`."""
        m = re.search(r"Search all ([\d,]+) items", html)
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None

    def extract_listings_from_json(
        self, html: str, source_url: str, seller_username: str
    ) -> list[EbayListing]:
        """Parse listings out of the embedded `$storenode_C` JSON payload."""
        payload = self._extract_storenode_payload(html)
        if payload is None:
            return []

        cards = self._find_listing_cards(payload)
        listings: list[EbayListing] = []
        for card in cards:
            try:
                listing = self._card_to_listing(card, source_url, seller_username)
                if listing is not None:
                    listings.append(listing)
            except Exception as e:
                logger.debug(f"Failed to parse card: {e}")
        return listings

    def extract_listings_from_dom(
        self, html: str, source_url: str, seller_username: str
    ) -> list[EbayListing]:
        """Fallback: extract listings from the DOM via regex on item cards."""
        listings: list[EbayListing] = []
        seen: set[str] = set()

        card_pattern = re.compile(
            r'<article[^>]*data-testid="ig-(\d+)"[^>]*>(.*?)</article>',
            re.DOTALL,
        )
        for match in card_pattern.finditer(html):
            listing_id = match.group(1)
            if listing_id in seen:
                continue
            seen.add(listing_id)
            body = match.group(2)

            title = None
            t = re.search(
                r'class="str-item-card__property-title[^"]*"[^>]*>.*?'
                r'<span[^>]*aria-hidden="true">([^<]+)',
                body,
                re.DOTALL,
            )
            if t:
                title = t.group(1).strip()

            url = None
            u = re.search(
                r'<a\s+[^>]*class="[^"]*str-item-card__link[^"]*"[^>]*href="([^"]+)"',
                body,
            )
            if u:
                url = u.group(1)

            image = None
            im = re.search(r'<source[^>]+srcset="([^"]+)"', body)
            if im:
                image = im.group(1)
            else:
                im2 = re.search(r'<img[^>]+src="([^"]+)"', body)
                if im2:
                    image = im2.group(1)

            display_price = None
            price_val: float | None = None
            p = re.search(
                r'class="[^"]*str-item-card__property-displayPrice[^"]*"[^>]*>([^<]+)',
                body,
            )
            if p:
                display_price = p.group(1).strip()
                price_val = _parse_price_number(display_price)

            listings.append(
                EbayListing(
                    listing_id=listing_id,
                    seller_username=seller_username,
                    title=title,
                    url=url,
                    image_url=image,
                    price=price_val,
                    display_price=display_price,
                    source_url=source_url,
                )
            )

        return listings

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _extract_storenode_payload(self, html: str) -> dict[str, Any] | None:
        """Locate the $storenode_C call and return its first-arg JSON object."""
        m = _STORENODE_START.search(html)
        if not m:
            return None
        start = m.end()
        end = _find_matching_brace(html, start)
        if end is None:
            return None
        try:
            return json.loads(html[start:end + 1])
        except json.JSONDecodeError as e:
            logger.debug(f"storenode JSON decode failed: {e}")
            return None

    def _find_listing_cards(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk `o.w` entries and return the first LISTINGS_MODULE's cards."""
        try:
            entries = payload.get("o", {}).get("w", [])
        except AttributeError:
            return []

        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            widget_data = entry[2]
            if not isinstance(widget_data, dict):
                continue
            modules = (
                widget_data.get("page", {})
                .get("modules", {})
                if isinstance(widget_data.get("page"), dict)
                else {}
            )
            listings_module = modules.get("LISTINGS_MODULE")
            if not isinstance(listings_module, dict):
                continue
            containers = listings_module.get("containers") or []
            if not containers:
                continue
            cards = containers[0].get("cards") or []
            if cards:
                return [c for c in cards if isinstance(c, dict)]
        return []

    def _card_to_listing(
        self,
        card: dict[str, Any],
        source_url: str,
        seller_username: str,
    ) -> EbayListing | None:
        listing_id = card.get("listingId") or card.get("id")
        if not listing_id:
            return None
        # Strip any suffix like "-NIL-NIL"
        listing_id = str(listing_id).split("-", 1)[0]

        title = _first_textspan(card.get("title"))

        action = card.get("action") or {}
        url = action.get("URL") if isinstance(action, dict) else None

        image = card.get("image") or {}
        image_url = image.get("URL") if isinstance(image, dict) else None

        display_price = _first_textspan(card.get("displayPrice"))
        price_val: float | None = None
        currency: str | None = None
        dp = card.get("displayPrice")
        if isinstance(dp, dict):
            value = dp.get("value")
            if isinstance(value, dict):
                pv = value.get("value")
                if isinstance(pv, (int, float)):
                    price_val = float(pv)
                currency = value.get("currency")
        if price_val is None and display_price:
            price_val = _parse_price_number(display_price)

        quantity: int | None = None
        qty_text = _first_textspan(card.get("quantity"))
        if qty_text:
            qm = re.search(r"(\d+)", qty_text)
            if qm:
                quantity = int(qm.group(1))

        shipping_cost = _first_textspan(card.get("logisticsCost"))

        condition: str | None = None
        brand: str | None = None
        search_meta = card.get("__search")
        if isinstance(search_meta, dict):
            cond = search_meta.get("normalizedCondition")
            if isinstance(cond, dict):
                condition = cond.get("text") or _first_textspan(cond)
            attrs = search_meta.get("displayAttributes")
            if isinstance(attrs, dict):
                tds = attrs.get("textualDisplays") or []
                if tds and isinstance(tds[0], dict):
                    brand = _first_textspan(tds[0])

        return EbayListing(
            listing_id=listing_id,
            seller_username=seller_username,
            title=title,
            url=url,
            image_url=image_url,
            price=price_val,
            currency=currency,
            display_price=display_price,
            condition=condition,
            quantity=quantity,
            shipping_cost=shipping_cost,
            brand=brand,
            source_url=source_url,
        )


def _first_textspan(node: Any) -> str | None:
    """Return the first `textSpans[].text` string from a TextualDisplay-like dict."""
    if not isinstance(node, dict):
        return None
    spans = node.get("textSpans")
    if isinstance(spans, list):
        for span in spans:
            if isinstance(span, dict):
                text = span.get("text")
                if isinstance(text, str) and text:
                    return text
    text = node.get("text")
    if isinstance(text, str) and text:
        return text
    return None


def _parse_price_number(display: str) -> float | None:
    """Extract a float from a display-price string like "$19.99" or "US $1,234.56"."""
    m = re.search(r"[\d,]+\.?\d*", display)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _find_matching_brace(text: str, start: int) -> int | None:
    """Return the index of the closing brace that matches `{` at `start`."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None
