"""
Pydantic models for the Xiaohongshu scraper.

Defines data structures for notes, comments, authors, and scraper results.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class XiaohongshuReply(BaseModel):
    """Reply to a Xiaohongshu comment (second-level comment).

    Attributes:
        reply_id: Unique reply identifier.
        content: Reply text content.
        author_name: Name of the reply author.
        author_id: User ID of the reply author.
        author_avatar: URL of author's avatar image.
        like_count: Number of likes on the reply.
        reply_to_user: Username of the user being replied to.
        reply_to_user_id: User ID of the user being replied to.
        created_at: When the reply was posted.
    """

    reply_id: str | None = None
    content: str | None = None
    author_name: str | None = None
    author_id: str | None = None
    author_avatar: str | None = None
    like_count: int | None = None
    reply_to_user: str | None = None
    reply_to_user_id: str | None = None
    created_at: datetime | None = None


class XiaohongshuComment(BaseModel):
    """Comment on a Xiaohongshu note (first-level comment with nested replies).

    Attributes:
        comment_id: Unique comment identifier.
        content: Comment text content.
        author_name: Name of the comment author.
        author_id: User ID of the comment author.
        author_avatar: URL of author's avatar image.
        like_count: Number of likes on the comment.
        reply_count: Number of replies to this comment.
        replies: List of nested replies.
        is_author_liked: Whether the note author liked this comment.
        ip_location: IP location of the comment author.
        created_at: When the comment was posted.
    """

    comment_id: str | None = None
    content: str | None = None
    author_name: str | None = None
    author_id: str | None = None
    author_avatar: str | None = None
    like_count: int | None = None
    reply_count: int | None = None
    replies: list[XiaohongshuReply] = Field(default_factory=list)
    is_author_liked: bool = False
    ip_location: str | None = None
    created_at: datetime | None = None


class XiaohongshuAuthor(BaseModel):
    """Xiaohongshu user/author profile information.

    Attributes:
        user_id: Unique user identifier.
        red_id: Xiaohongshu number.
        nickname: Display name of the user.
        avatar_url: URL of the user's avatar image.
        description: User's profile bio/description.
        follower_count: Number of followers.
        following_count: Number of users being followed.
        note_count: Total number of notes published.
        verified: Whether the account is verified.
    """

    user_id: str
    red_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    note_count: int | None = None
    verified: bool = False


class XiaohongshuNote(BaseModel):
    """Individual Xiaohongshu note/post.

    Attributes:
        note_id: Unique note identifier.
        title: Note title.
        content: Full text content of the note.
        author_id: User ID of the note author.
        author_name: Display name of the author.
        image_urls: List of original image URLs.
        image_paths: List of downloaded image paths (local or S3).
        video_url: Video URL if this is a video note.
        like_count: Number of likes.
        collect_count: Number of saves/collections.
        comment_count: Number of comments.
        comments: List of extracted comments.
        tags: List of hashtags on the note.
        location: Location tag if present.
        source_url: URL of the note page.
        created_at: When the note was published.
    """

    note_id: str
    title: str | None = None
    content: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    image_paths: list[str] = Field(default_factory=list)
    video_url: str | None = None
    like_count: int | None = None
    collect_count: int | None = None
    comment_count: int | None = None
    comments: list[XiaohongshuComment] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    location: str | None = None
    source_url: str | None = None
    source_html_path: str | None = None
    created_at: datetime | None = None


class XiaohongshuResult(ScraperResult):
    """Result from Xiaohongshu scraper.

    Attributes:
        account_id: The account ID that was scraped.
        author: Author profile information.
        notes: List of extracted notes.
        notes_count: Total number of notes extracted.
        images_downloaded: Number of images downloaded.
        s3_uploaded: Whether images were uploaded to S3.
        login_required: Whether login was required to continue.
        login_screenshot_path: Path to login screenshot if captured.
    """

    account_id: str = ""
    author: XiaohongshuAuthor | None = None
    notes: list[XiaohongshuNote] = Field(default_factory=list)
    notes_count: int = 0
    images_downloaded: int = 0
    s3_uploaded: bool = False
    login_required: bool = False
    login_screenshot_path: str | None = None


class XiaohongshuFollowing(BaseModel):
    """Information about a followed user on Xiaohongshu.

    Attributes:
        user_id: The followed user's unique ID.
        nickname: Display name of the followed user.
        avatar_url: URL of the user's avatar image.
        red_id: Xiaohongshu number.
        description: User's profile bio/description.
        verified: Whether the account is verified.
        follower_count: Number of followers.
        note_count: Total number of notes published.
    """

    user_id: str
    nickname: str | None = None
    avatar_url: str | None = None
    red_id: str | None = None
    description: str | None = None
    verified: bool = False
    follower_count: int | None = None
    note_count: int | None = None


class XiaohongshuFollowingResult(ScraperResult):
    """Result from Xiaohongshu following list scraper.

    Attributes:
        account_id: The account ID whose following list was scraped.
        following: List of followed users.
        following_count: Number of users extracted.
        total_following: Total following count shown on profile.
        login_required: Whether login was required to continue.
        login_screenshot_path: Path to login screenshot if captured.
    """

    account_id: str = ""
    following: list[XiaohongshuFollowing] = Field(default_factory=list)
    following_count: int = 0
    total_following: int | None = None
    login_required: bool = False
    login_screenshot_path: str | None = None


class XiaohongshuSearchAuthorResult(ScraperResult):
    """Result from Xiaohongshu search author scraper.

    Searches for a keyword (e.g., aircraft registration) and extracts
    authors from the note cards in search results.

    Attributes:
        keyword: The search keyword used.
        authors: List of extracted author profiles.
        authors_count: Number of unique authors extracted.
        notes_scanned: Number of note cards scanned.
        login_required: Whether login was required to continue.
        login_screenshot_path: Path to login screenshot if captured.
    """

    keyword: str = ""
    authors: list[XiaohongshuAuthor] = Field(default_factory=list)
    authors_count: int = 0
    notes_scanned: int = 0
    login_required: bool = False
    login_screenshot_path: str | None = None
