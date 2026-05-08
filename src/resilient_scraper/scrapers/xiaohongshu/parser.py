"""
Xiaohongshu data parsing and extraction logic.

Pure parsing module with no browser, database, or configuration dependencies.
All methods accept DOM elements or raw HTML/text and return parsed data structures.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from resilient_scraper.scrapers.xiaohongshu.models import (
    XiaohongshuAuthor,
    XiaohongshuComment,
    XiaohongshuFollowing,
    XiaohongshuReply,
)

logger = logging.getLogger("scraper.xiaohongshu.parser")


class XiaohongshuParser:
    """Pure parsing logic for Xiaohongshu pages. No browser or DB dependencies."""

    def extract_author_info(
        self, html: str, url: str, account_id: str
    ) -> XiaohongshuAuthor | None:
        """Extract author profile information from page HTML.

        Args:
            html: Raw HTML string of the profile page.
            url: Current page URL (used to extract user ID).
            account_id: Account ID being scraped.

        Returns:
            XiaohongshuAuthor or None if extraction fails.
        """
        try:
            # Extract user ID from URL
            user_id_match = re.search(r'/user/profile/([^/?]+)', url)
            user_id = user_id_match.group(1) if user_id_match else account_id

            author = XiaohongshuAuthor(user_id=user_id)

            # Extract nickname
            nickname_patterns = [
                r'class="[^"]*user-name[^"]*"[^>]*>([^<]+)',
                r'class="[^"]*nickname[^"]*"[^>]*>([^<]+)',
                r'"nickname"\s*:\s*"([^"]+)"',
            ]
            for pattern in nickname_patterns:
                match = re.search(pattern, html)
                if match:
                    author.nickname = match.group(1).strip()
                    break

            # Extract avatar URL
            avatar_patterns = [
                r'class="[^"]*avatar[^"]*"[^>]*src="([^"]+)"',
                r'"avatar"\s*:\s*"([^"]+)"',
            ]
            for pattern in avatar_patterns:
                match = re.search(pattern, html)
                if match:
                    author.avatar_url = match.group(1)
                    break

            # Extract follower count (粉丝)
            # XiaoHongShu formats: "粉丝</span><span>591" or "粉丝 591" (label before number)
            # Also try: "591 粉丝" but only if not preceded by another label
            follower_patterns = [
                r'粉丝</span>\s*<span[^>]*>(\d+(?:\.\d+)?[万亿]?)',  # 粉丝</span><span>591
                r'粉丝\s*</span>\s*<span[^>]*>\s*(\d+(?:\.\d+)?[万亿]?)',  # With extra spaces
                r'>粉丝<[^>]*>[^<]*(\d+(?:\.\d+)?[万亿]?)',  # >粉丝<...>591
                r'粉丝[^\d]{0,10}(\d+(?:\.\d+)?[万亿]?)',  # 粉丝 followed by number (within 10 chars)
                r'"fansCount"\s*:\s*["\']?(\d+)',
                r'"followerCount"\s*:\s*["\']?(\d+)',
            ]
            for pattern in follower_patterns:
                match = re.search(pattern, html)
                if match:
                    author.follower_count = self.parse_count(match.group(1))
                    break

            # Extract following count (关注)
            # XiaoHongShu formats: "关注</span><span>58" or "关注 58" (label before number)
            following_patterns = [
                r'关注</span>\s*<span[^>]*>(\d+(?:\.\d+)?[万亿]?)',  # 关注</span><span>58
                r'关注\s*</span>\s*<span[^>]*>\s*(\d+(?:\.\d+)?[万亿]?)',  # With extra spaces
                r'>关注<[^>]*>[^<]*(\d+(?:\.\d+)?[万亿]?)',  # >关注<...>58
                r'关注[^\d]{0,10}(\d+(?:\.\d+)?[万亿]?)',  # 关注 followed by number (within 10 chars)
                r'"followingCount"\s*:\s*["\']?(\d+)',
            ]
            for pattern in following_patterns:
                match = re.search(pattern, html)
                if match:
                    author.following_count = self.parse_count(match.group(1))
                    break

            # Extract description
            desc_patterns = [
                r'class="[^"]*desc[^"]*"[^>]*>([^<]+)',
                r'"description"\s*:\s*"([^"]+)"',
            ]
            for pattern in desc_patterns:
                match = re.search(pattern, html)
                if match:
                    author.description = match.group(1).strip()[:500]
                    break

            logger.info(
                f"[{account_id}] Extracted author: {author.nickname} ({author.user_id}) - "
                f"粉丝:{author.follower_count}, 关注:{author.following_count}"
            )

            return author

        except Exception as e:
            logger.error(f"[{account_id}] Error extracting author info: {e}")
            return None

    def parse_count(self, count_str: str) -> int:
        """Parse count string with Chinese units (万, 亿).

        Args:
            count_str: Count string like "1.5万" or "1234".

        Returns:
            Integer count value.
        """
        try:
            count_str = count_str.strip()
            if "亿" in count_str:
                num = float(count_str.replace("亿", ""))
                return int(num * 100000000)
            elif "万" in count_str:
                num = float(count_str.replace("万", ""))
                return int(num * 10000)
            else:
                return int(float(count_str))
        except (ValueError, TypeError):
            return 0

    def parse_relative_date(self, date_str: str) -> datetime | None:
        """Parse relative date string to absolute datetime.

        Supports formats like:
        - "2天前", "3小时前", "5分钟前", "刚刚"
        - "昨天", "前天"
        - "2025-01-15", "01-15", "1月15日"
        - "编辑于 2天前", "发布于 昨天"

        Args:
            date_str: Date string from the page.

        Returns:
            datetime object or None if parsing fails.
        """
        if not date_str:
            return None

        try:
            date_str = date_str.strip()
            # Remove common prefixes
            for prefix in ["编辑于", "发布于", "更新于", " "]:
                date_str = date_str.replace(prefix, "").strip()

            now = datetime.now(timezone.utc)

            # Relative time patterns
            if "刚刚" in date_str or "秒前" in date_str:
                return now

            if "分钟前" in date_str:
                match = re.search(r"(\d+)\s*分钟前", date_str)
                if match:
                    minutes = int(match.group(1))
                    return now - timedelta(minutes=minutes)

            if "小时前" in date_str:
                match = re.search(r"(\d+)\s*小时前", date_str)
                if match:
                    hours = int(match.group(1))
                    return now - timedelta(hours=hours)

            if "天前" in date_str:
                match = re.search(r"(\d+)\s*天前", date_str)
                if match:
                    days = int(match.group(1))
                    return now - timedelta(days=days)

            if "周前" in date_str:
                match = re.search(r"(\d+)\s*周前", date_str)
                if match:
                    weeks = int(match.group(1))
                    return now - timedelta(weeks=weeks)

            if "月前" in date_str:
                match = re.search(r"(\d+)\s*月前", date_str)
                if match:
                    months = int(match.group(1))
                    return now - timedelta(days=months * 30)

            if "年前" in date_str:
                match = re.search(r"(\d+)\s*年前", date_str)
                if match:
                    years = int(match.group(1))
                    return now - timedelta(days=years * 365)

            if date_str == "昨天":
                return now - timedelta(days=1)

            if date_str == "前天":
                return now - timedelta(days=2)

            # Absolute date patterns
            # Format: 2025-01-15 or 2025/01/15
            match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", date_str)
            if match:
                year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                return datetime(year, month, day, tzinfo=timezone.utc)

            # Format: 01-15 or 01/15 (current year assumed)
            match = re.search(r"^(\d{1,2})[-/](\d{1,2})$", date_str)
            if match:
                month, day = int(match.group(1)), int(match.group(2))
                return datetime(now.year, month, day, tzinfo=timezone.utc)

            # Format: 1月15日 or 12月5日
            match = re.search(r"(\d{1,2})月(\d{1,2})日", date_str)
            if match:
                month, day = int(match.group(1)), int(match.group(2))
                return datetime(now.year, month, day, tzinfo=timezone.utc)

            logger.debug(f"Could not parse date: {date_str}")
            return None

        except Exception as e:
            logger.debug(f"Error parsing date '{date_str}': {e}")
            return None

    def extract_note_id_from_element(self, element: Any) -> str | None:
        """Extract note ID from element's attributes.

        Tries multiple strategies to find the note ID:
        1. Element's href attribute with /explore/ pattern
        2. Child anchor element's href
        3. Element's data-id or data-note-id attribute
        4. Any attribute containing note ID pattern

        Args:
            element: The note DOM element (usually an anchor tag or section).

        Returns:
            Note ID string or None if not found.
        """
        note_id_pattern = re.compile(r"[a-f0-9]{24}")  # Xiaohongshu note IDs are 24 hex chars

        try:
            # Strategy 1: Direct href attribute
            href = element.attr("href")
            if href and "/explore/" in href:
                note_id = href.split("/explore/")[-1].split("?")[0]
                if note_id and note_id_pattern.match(note_id):
                    return note_id

            # Strategy 2: Child anchor element's href
            try:
                child_links = element.eles("tag:a", timeout=0.5)
                for link in child_links:
                    link_href = link.attr("href")
                    if link_href and "/explore/" in link_href:
                        note_id = link_href.split("/explore/")[-1].split("?")[0]
                        if note_id and note_id_pattern.match(note_id):
                            return note_id
            except Exception as e:
                logger.debug(f"Strategy 2 failed: {e}")

            # Strategy 3: Data attributes
            for attr_name in ["data-id", "data-note-id", "id"]:
                try:
                    attr_value = element.attr(attr_name)
                    if attr_value and note_id_pattern.match(attr_value):
                        return attr_value
                except Exception:
                    pass

            # Strategy 4: Check element's outer HTML for note ID pattern
            try:
                outer_html = element.html
                if outer_html:
                    # Look for /explore/{note_id} or /discovery/item/{note_id} pattern
                    match = re.search(r"(?:/explore/|/discovery/item/)([a-f0-9]{24})", outer_html)
                    if match:
                        return match.group(1)
                    # Also try standalone 24-char hex pattern if it looks like a note ID
                    # Only log for debugging - don't use this as it might match other IDs
                    if outer_html and len(outer_html) < 2000:
                        logger.debug(f"Note element HTML (no note_id found): {outer_html[:500]}...")
            except Exception:
                pass

        except Exception:
            pass
        return None

    def extract_single_comment(
        self,
        comment_element: Any,
        account_id: str,
    ) -> XiaohongshuComment | None:
        """Extract a single comment from a DOM element.

        Parses comment content, author info, like count, IP location,
        and author-liked status from the comment element.

        Args:
            comment_element: The comment DOM element.
            account_id: Account ID for logging.

        Returns:
            XiaohongshuComment or None if extraction fails.
        """
        try:
            comment = XiaohongshuComment()

            # Extract comment ID from element id attribute (format: comment-xxxxx)
            try:
                comment_id = comment_element.attr("id")
                if comment_id and comment_id.startswith("comment-"):
                    comment.comment_id = comment_id.replace("comment-", "")
                else:
                    comment.comment_id = comment_id
            except Exception:
                pass

            # Extract comment content - based on actual HTML structure
            # Structure: .content > .note-text > span (actual text)
            try:
                content_elem = comment_element.ele("css:.content .note-text", timeout=0.1)
                if content_elem and content_elem.text.strip():
                    comment.content = content_elem.text.strip()
            except Exception:
                pass

            # Extract author name - from .author-wrapper .author .name
            try:
                name_elem = comment_element.ele("css:.author-wrapper .author .name", timeout=0.1)
                if name_elem and name_elem.text.strip():
                    comment.author_name = name_elem.text.strip()
            except Exception:
                pass

            # Extract author ID from data-user-id attribute on name link
            try:
                name_link = comment_element.ele("css:.author .name", timeout=0.1)
                if name_link:
                    user_id = name_link.attr("data-user-id")
                    if user_id:
                        comment.author_id = user_id
            except Exception:
                pass

            # Extract author avatar - from .avatar .avatar-item
            try:
                avatar_elem = comment_element.ele("css:.avatar .avatar-item", timeout=0.1)
                if avatar_elem:
                    avatar_url = avatar_elem.attr("src")
                    if avatar_url and avatar_url.startswith("http"):
                        comment.author_avatar = avatar_url
            except Exception:
                pass

            # Extract like count - from .interactions .like .like-wrapper .count
            try:
                like_elem = comment_element.ele("css:.interactions .like-wrapper .count", timeout=0.1)
                if like_elem and like_elem.text.strip():
                    like_text = like_elem.text.strip()
                    if like_text and like_text != "赞":
                        comment.like_count = self.parse_count(like_text)
            except Exception:
                pass

            # Extract IP location - from .info .date .location
            try:
                location_elem = comment_element.ele("css:.info .date .location", timeout=0.1)
                if location_elem and location_elem.text.strip():
                    comment.ip_location = location_elem.text.strip()
            except Exception:
                pass

            # Check if author liked this comment - look for like-active class
            try:
                liked_elem = comment_element.ele("css:.like-wrapper.like-active", timeout=0.1)
                if liked_elem:
                    comment.is_author_liked = True
            except Exception:
                pass

            # Return if we have content OR author_name (some comments are just @mentions)
            if comment.content or comment.author_name:
                content_preview = comment.content[:30] if comment.content else "(no content)"
                logger.debug(
                    f"[{account_id}] Extracted comment: {comment.author_name} - "
                    f"{content_preview}... (likes: {comment.like_count})"
                )
                return comment

            return None

        except Exception as e:
            logger.debug(f"[{account_id}] Error extracting single comment: {e}")
            return None

    def extract_single_reply(
        self,
        reply_element: Any,
        account_id: str,
    ) -> XiaohongshuReply | None:
        """Extract a single reply (sub-comment) from a DOM element.

        Sub-comments have the same HTML structure as main comments,
        just with class "comment-item-sub" instead of "comment-item".

        Args:
            reply_element: The reply DOM element.
            account_id: Account ID for logging.

        Returns:
            XiaohongshuReply or None if extraction fails.
        """
        try:
            reply = XiaohongshuReply()

            # Extract reply ID from id attribute (format: comment-xxxxx)
            try:
                reply_id = reply_element.attr("id")
                if reply_id and reply_id.startswith("comment-"):
                    reply.reply_id = reply_id.replace("comment-", "")
                else:
                    reply.reply_id = reply_id
            except Exception:
                pass

            # Extract content - same structure as main comment
            try:
                content_elem = reply_element.ele("css:.content .note-text", timeout=0.1)
                if content_elem and content_elem.text.strip():
                    reply.content = content_elem.text.strip()
            except Exception:
                pass

            # Extract author name
            try:
                name_elem = reply_element.ele("css:.author-wrapper .author .name", timeout=0.1)
                if name_elem and name_elem.text.strip():
                    reply.author_name = name_elem.text.strip()
            except Exception:
                pass

            # Extract author ID from data-user-id attribute
            try:
                name_link = reply_element.ele("css:.author .name", timeout=0.1)
                if name_link:
                    user_id = name_link.attr("data-user-id")
                    if user_id:
                        reply.author_id = user_id
            except Exception:
                pass

            # Extract author avatar
            try:
                avatar_elem = reply_element.ele("css:.avatar .avatar-item", timeout=0.1)
                if avatar_elem:
                    avatar_url = avatar_elem.attr("src")
                    if avatar_url and avatar_url.startswith("http"):
                        reply.author_avatar = avatar_url
            except Exception:
                pass

            # Extract like count
            try:
                like_elem = reply_element.ele("css:.interactions .like-wrapper .count", timeout=0.1)
                if like_elem and like_elem.text.strip():
                    like_text = like_elem.text.strip()
                    if like_text and like_text != "赞":
                        reply.like_count = self.parse_count(like_text)
            except Exception:
                pass

            # Return if we have content
            if reply.content:
                logger.debug(
                    f"[{account_id}] Extracted reply: {reply.author_name} - "
                    f"{reply.content[:30]}..."
                )
                return reply

            return None

        except Exception as e:
            logger.debug(f"[{account_id}] Error extracting single reply: {e}")
            return None

    def detect_image_type(self, content: bytes, content_type: str, url: str) -> str:
        """Detect actual image type from content bytes, headers, or URL.

        Uses magic bytes first, then falls back to Content-Type header,
        and finally to URL patterns.

        Args:
            content: File content bytes.
            content_type: HTTP Content-Type header value.
            url: Original URL for fallback detection.

        Returns:
            File extension string (jpg, png, webp, etc.).
        """
        # Check magic bytes first
        if len(content) >= 12:
            if content[:3] == b'\xff\xd8\xff':
                return "jpg"
            if content[:4] == b'\x89PNG':
                return "png"
            if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
                return "webp"
            if content[:6] in (b'GIF87a', b'GIF89a'):
                return "gif"
            if content[4:8] == b'ftyp':
                # Could be AVIF or HEIF
                if b'avif' in content[8:16]:
                    return "avif"
                return "heic"

        # Fall back to Content-Type header
        if "webp" in content_type:
            return "webp"
        if "png" in content_type:
            return "png"
        if "gif" in content_type:
            return "gif"
        if "avif" in content_type:
            return "avif"

        # Fall back to URL
        url_lower = url.lower()
        if ".webp" in url_lower or "_webp" in url_lower:
            return "webp"
        if ".png" in url_lower:
            return "png"
        if ".gif" in url_lower:
            return "gif"

        return "jpg"

    def extract_single_following(
        self,
        element: Any,
        account_id: str,
    ) -> XiaohongshuFollowing | None:
        """Extract a single following user from a DOM element.

        Args:
            element: The user item DOM element.
            account_id: Account ID being scraped (for logging).

        Returns:
            XiaohongshuFollowing or None if extraction fails.
        """
        try:
            user_id = None
            nickname = None
            avatar_url = None
            red_id = None
            description = None
            verified = False
            follower_count = None
            note_count = None

            # Extract user ID from href
            try:
                link = element.ele("css:a[href*='/user/profile/']", timeout=0.5)
                if link:
                    href = link.attr("href")
                    match = re.search(r'/user/profile/([^/?]+)', href)
                    if match:
                        user_id = match.group(1)
            except Exception:
                pass

            # Fallback: try data attribute
            if not user_id:
                try:
                    user_id = element.attr("data-user-id")
                except Exception:
                    pass

            if not user_id:
                return None

            # Extract nickname
            try:
                name_selectors = [
                    "css:.name",
                    "css:.nickname",
                    "css:.user-name",
                    "xpath:.//span[contains(@class, 'name')]",
                ]
                for sel in name_selectors:
                    try:
                        name_elem = element.ele(sel, timeout=0.3)
                        if name_elem and name_elem.text.strip():
                            nickname = name_elem.text.strip()
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # Extract avatar URL
            try:
                avatar_elem = element.ele("css:img.avatar,css:.avatar img", timeout=0.3)
                if avatar_elem:
                    avatar_url = avatar_elem.attr("src")
            except Exception:
                pass

            # Extract description/bio
            try:
                desc_selectors = ["css:.desc", "css:.bio", "css:.description"]
                for sel in desc_selectors:
                    try:
                        desc_elem = element.ele(sel, timeout=0.3)
                        if desc_elem and desc_elem.text.strip():
                            description = desc_elem.text.strip()[:500]
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # Check for verification badge
            try:
                verify_elem = element.ele("css:.verify,css:.verified,css:.badge", timeout=0.3)
                if verify_elem:
                    verified = True
            except Exception:
                pass

            # Extract follower count
            try:
                follower_elem = element.ele("xpath:.//span[contains(text(), '粉丝')]", timeout=0.3)
                if follower_elem:
                    text = follower_elem.text
                    match = re.search(r'(\d+(?:\.\d+)?[万亿]?)', text)
                    if match:
                        follower_count = self.parse_count(match.group(1))
            except Exception:
                pass

            # Extract note count
            try:
                note_elem = element.ele("xpath:.//span[contains(text(), '笔记')]", timeout=0.3)
                if note_elem:
                    text = note_elem.text
                    match = re.search(r'(\d+(?:\.\d+)?[万亿]?)', text)
                    if match:
                        note_count = self.parse_count(match.group(1))
            except Exception:
                pass

            following = XiaohongshuFollowing(
                user_id=user_id,
                nickname=nickname,
                avatar_url=avatar_url,
                red_id=red_id,
                description=description,
                verified=verified,
                follower_count=follower_count,
                note_count=note_count,
            )

            logger.debug(
                f"[{account_id}] Extracted following: {nickname} ({user_id})"
            )
            return following

        except Exception as e:
            logger.debug(f"[{account_id}] Error extracting following user: {e}")
            return None

    def extract_author_from_note_card(
        self,
        element: Any,
        keyword: str,
    ) -> XiaohongshuAuthor | None:
        """Extract author information from a search result note card element.

        Args:
            element: The note card DOM element.
            keyword: Search keyword for logging.

        Returns:
            XiaohongshuAuthor or None if extraction fails.
        """
        try:
            user_id = None
            nickname = None
            avatar_url = None

            # Method 1: Extract user ID from author link
            author_link_selectors = [
                "css:a.author[href*='/user/profile/']",
                "css:a[href*='/user/profile/']",
                "xpath:.//a[contains(@href, '/user/profile/')]",
            ]

            for selector in author_link_selectors:
                try:
                    link = element.ele(selector, timeout=0.5)
                    if link:
                        href = link.attr("href")
                        match = re.search(r'/user/profile/([^/?]+)', href)
                        if match:
                            user_id = match.group(1)
                            break
                except Exception:
                    continue

            # Method 2: Try data attribute
            if not user_id:
                try:
                    user_id = element.attr("data-user-id")
                except Exception:
                    pass

            # Method 3: Extract from avatar link
            if not user_id:
                try:
                    avatar_link = element.ele("css:a[href*='/user/profile/'] img", timeout=0.3)
                    if avatar_link:
                        parent = avatar_link.parent()
                        if parent:
                            href = parent.attr("href")
                            if href:
                                match = re.search(r'/user/profile/([^/?]+)', href)
                                if match:
                                    user_id = match.group(1)
                except Exception:
                    pass

            if not user_id:
                return None

            # Extract nickname
            nickname_selectors = [
                "css:.author-wrapper .name",
                "css:.author .name",
                "css:a.author span.name",
                "css:span.name",
                "xpath:.//span[contains(@class, 'name')]",
                "xpath:.//a[contains(@class, 'author')]//text()",
            ]

            for selector in nickname_selectors:
                try:
                    name_elem = element.ele(selector, timeout=0.3)
                    if name_elem and name_elem.text.strip():
                        nickname = name_elem.text.strip()
                        break
                except Exception:
                    continue

            # Extract avatar URL
            avatar_selectors = [
                "css:.author-wrapper img",
                "css:.author img",
                "css:a.author img",
                "css:img.avatar",
                "xpath:.//img[contains(@class, 'avatar')]",
            ]

            for selector in avatar_selectors:
                try:
                    avatar_elem = element.ele(selector, timeout=0.3)
                    if avatar_elem:
                        avatar_url = avatar_elem.attr("src")
                        if avatar_url:
                            break
                except Exception:
                    continue

            return XiaohongshuAuthor(
                user_id=user_id,
                nickname=nickname,
                avatar_url=avatar_url,
            )

        except Exception as e:
            logger.debug(f"[{keyword}] Error extracting author from note card: {e}")
            return None

    # ===================================================================
    # __INITIAL_STATE__ extraction (more reliable than DOM parsing)
    # ===================================================================

    def extract_authors_from_initial_state(
        self,
        initial_state: dict[str, Any],
        keyword: str,
    ) -> list[XiaohongshuAuthor]:
        """Extract authors from XHS __INITIAL_STATE__ search feeds data.

        The __INITIAL_STATE__ object contains structured JSON data for search
        results, which is more stable than DOM structure changes.

        Args:
            initial_state: Parsed __INITIAL_STATE__ object from page JS.
            keyword: Search keyword for logging.

        Returns:
            List of extracted XiaohongshuAuthor objects.
        """
        authors: list[XiaohongshuAuthor] = []
        seen_user_ids: set[str] = set()

        try:
            # Navigate to search feeds — handles both .value and direct access
            feeds = self._get_nested_value(initial_state, ["search", "feeds"])
            if not feeds:
                logger.debug(f"[{keyword}] No search.feeds in __INITIAL_STATE__")
                return authors

            if not isinstance(feeds, list):
                logger.debug(f"[{keyword}] search.feeds is not a list: {type(feeds)}")
                return authors

            for feed in feeds:
                try:
                    # Extract note_card data
                    note_card = feed.get("note_card") or feed.get("noteCard") or feed
                    user = note_card.get("user") or {}

                    user_id = user.get("user_id") or user.get("userId") or ""
                    if not user_id or user_id in seen_user_ids:
                        continue

                    seen_user_ids.add(user_id)
                    nickname = user.get("nickname") or user.get("nick_name") or ""
                    avatar = user.get("avatar") or ""

                    author = XiaohongshuAuthor(
                        user_id=user_id,
                        nickname=nickname,
                        avatar_url=avatar,
                    )
                    authors.append(author)

                except Exception as e:
                    logger.debug(f"[{keyword}] Error parsing feed entry: {e}")
                    continue

            logger.info(
                f"[{keyword}] __INITIAL_STATE__: extracted {len(authors)} authors "
                f"from {len(feeds)} feeds"
            )

        except Exception as e:
            logger.warning(f"[{keyword}] Error parsing __INITIAL_STATE__: {e}")

        return authors

    def _get_nested_value(
        self, obj: dict[str, Any], keys: list[str]
    ) -> Any:
        """Get a nested value from a dict, handling XHS's .value/_value wrapper pattern.

        XHS __INITIAL_STATE__ sometimes wraps values in {value: X} or {_value: X}.
        This method transparently unwraps them.

        Args:
            obj: Dictionary to traverse.
            keys: List of keys to follow.

        Returns:
            The value at the nested path, or None if not found.
        """
        current = obj
        for key in keys:
            if not isinstance(current, dict):
                return None

            if key in current:
                current = current[key]
            else:
                return None

            # Unwrap .value / ._value pattern
            if isinstance(current, dict):
                if "value" in current and len(current) <= 2:
                    current = current["value"]
                elif "_value" in current and len(current) <= 2:
                    current = current["_value"]

        return current
