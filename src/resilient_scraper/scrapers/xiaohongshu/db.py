"""
Database operations for the Xiaohongshu scraper.

Encapsulates all database interaction logic including table creation,
author/note persistence, and utility queries.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from resilient_scraper.scrapers.xiaohongshu.models import (
    XiaohongshuAuthor,
    XiaohongshuFollowing,
    XiaohongshuNote,
)

logger = logging.getLogger("scraper.xiaohongshu")


class XiaohongshuDB:
    """Database operations for Xiaohongshu scraper data.

    Handles table creation, author/note persistence, and following
    relationship storage for the Xiaohongshu scraper.

    Args:
        db_engine: SQLAlchemy database engine instance.
        s3_bucket: S3 bucket name for image storage references.
        s3_prefix: S3 key prefix for image storage references.
    """

    def __init__(
        self,
        db_engine: Engine | None,
        s3_bucket: str = "",
        s3_prefix: str = "",
    ) -> None:
        self.db_engine = db_engine
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix

    def ensure_tables_exist(self) -> None:
        """Create database tables if they don't exist."""
        if not self.db_engine:
            return

        try:
            with self.db_engine.connect() as conn:
                # Create xiaohongshu_authors table
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS xiaohongshu_authors (
                        id BIGSERIAL PRIMARY KEY,
                        user_id VARCHAR(50) UNIQUE NOT NULL,
                        red_id VARCHAR(50),
                        nickname VARCHAR(100),
                        avatar_url VARCHAR(500),
                        description TEXT,
                        follower_count INTEGER,
                        following_count INTEGER,
                        note_count INTEGER,
                        verified BOOLEAN DEFAULT FALSE,
                        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        discovered_from_registrations TEXT[] DEFAULT '{}'
                    )
                """))

                # Add discovered_from_registrations column if missing (migration)
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'xiaohongshu_authors'
                            AND column_name = 'discovered_from_registrations'
                        ) THEN
                            ALTER TABLE xiaohongshu_authors
                            ADD COLUMN discovered_from_registrations TEXT[] DEFAULT '{}';
                        END IF;
                    END $$;
                """))

                # Add red_id column if missing (migration)
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'xiaohongshu_authors'
                            AND column_name = 'red_id'
                        ) THEN
                            ALTER TABLE xiaohongshu_authors
                            ADD COLUMN red_id VARCHAR(50);
                        END IF;
                    END $$;
                """))

                # Create xiaohongshu_notes table
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS xiaohongshu_notes (
                        id BIGSERIAL PRIMARY KEY,
                        note_id VARCHAR(50) UNIQUE NOT NULL,
                        source_url VARCHAR(500),
                        title VARCHAR(500),
                        content TEXT,
                        tags JSONB,
                        location VARCHAR(200),
                        author_id VARCHAR(50) NOT NULL,
                        author_name VARCHAR(100),
                        image_urls JSONB,
                        image_paths JSONB,
                        video_url VARCHAR(500),
                        like_count INTEGER,
                        collect_count INTEGER,
                        comment_count INTEGER,
                        share_count INTEGER,
                        comments JSONB,
                        note_created_at TIMESTAMP,
                        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))

                # Add source_html_path column if missing (migration)
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'xiaohongshu_notes'
                            AND column_name = 'source_html_path'
                        ) THEN
                            ALTER TABLE xiaohongshu_notes
                            ADD COLUMN source_html_path VARCHAR(500);
                        END IF;
                    END $$;
                """))

                # Create indexes
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_xiaohongshu_notes_author_id
                    ON xiaohongshu_notes(author_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_xiaohongshu_notes_scraped_at
                    ON xiaohongshu_notes(scraped_at)
                """))

                # Create xiaohongshu_cycle_state table for cycle scheduler
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS xiaohongshu_cycle_state (
                        id BIGSERIAL PRIMARY KEY,
                        cycle_id VARCHAR(50) UNIQUE NOT NULL,
                        status VARCHAR(20) DEFAULT 'running',
                        current_phase VARCHAR(50),

                        -- Phase 1: Trending aircraft
                        target_registrations JSONB,
                        searched_registrations JSONB DEFAULT '[]',

                        -- Phase 2: Author search
                        discovered_authors JSONB DEFAULT '[]',

                        -- Phase 3: Note scraping
                        target_authors JSONB,
                        scraped_authors JSONB DEFAULT '[]',

                        -- Phase 4: Note analysis
                        notes_to_analyze INTEGER DEFAULT 0,
                        notes_analyzed INTEGER DEFAULT 0,

                        -- Statistics
                        total_authors_found INTEGER DEFAULT 0,
                        total_notes_scraped INTEGER DEFAULT 0,
                        total_registrations_found INTEGER DEFAULT 0,

                        -- Timestamps
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                        -- Error info
                        error_message TEXT
                    )
                """))

                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_xiaohongshu_cycle_state_cycle_id
                    ON xiaohongshu_cycle_state(cycle_id)
                """))

                conn.commit()
                logger.info("Xiaohongshu database tables ensured")

        except SQLAlchemyError as e:
            logger.error(f"Failed to create tables: {e}")

    def load_existing_note_ids(self, author_id: str, days: int = 0) -> set[str]:
        """Load existing note IDs to skip redundant scraping.

        Args:
            author_id: The author's user ID.
            days: Only skip notes scraped within N days. 0 means skip all existing.

        Returns:
            Set of note IDs that should be skipped.
        """
        if not self.db_engine:
            return set()

        try:
            with self.db_engine.connect() as conn:
                if days > 0:
                    query = text("""
                        SELECT note_id FROM xiaohongshu_notes
                        WHERE author_id = :author_id
                        AND scraped_at > NOW() - MAKE_INTERVAL(days => :days)
                    """)
                    params: dict[str, Any] = {"author_id": author_id, "days": days}
                    label = f"within {days} days"
                else:
                    query = text("""
                        SELECT note_id FROM xiaohongshu_notes
                        WHERE author_id = :author_id
                    """)
                    params = {"author_id": author_id}
                    label = "all time"
                result = conn.execute(query, params)
                note_ids = {row[0] for row in result}
                logger.info(
                    f"[{author_id}] Loaded {len(note_ids)} existing note IDs ({label})"
                )
                return note_ids
        except Exception as e:
            logger.warning(f"[{author_id}] Failed to load existing note IDs: {e}")
            return set()

    def save_author(
        self, author: XiaohongshuAuthor, discovered_from: str | None = None
    ) -> bool:
        """Save author profile to database.

        Args:
            author: Author profile to save.
            discovered_from: Registration number that led to discovering this author.

        Returns:
            True if saved successfully, False otherwise.
        """
        if not self.db_engine:
            return False

        try:
            with self.db_engine.connect() as conn:
                conn.execute(
                    text("""
                        INSERT INTO xiaohongshu_authors (
                            user_id, red_id, nickname, avatar_url, description,
                            follower_count, following_count, note_count, verified,
                            scraped_at, updated_at, discovered_from_registrations
                        ) VALUES (
                            :user_id, :red_id, :nickname, :avatar_url, :description,
                            :follower_count, :following_count, :note_count, :verified,
                            :scraped_at, :updated_at,
                            CASE WHEN :discovered_from IS NOT NULL
                                THEN ARRAY[:discovered_from]
                                ELSE '{}'
                            END
                        )
                        ON CONFLICT (user_id) DO UPDATE SET
                            red_id = COALESCE(EXCLUDED.red_id, xiaohongshu_authors.red_id),
                            nickname = COALESCE(EXCLUDED.nickname, xiaohongshu_authors.nickname),
                            avatar_url = COALESCE(EXCLUDED.avatar_url, xiaohongshu_authors.avatar_url),
                            description = COALESCE(EXCLUDED.description, xiaohongshu_authors.description),
                            follower_count = COALESCE(EXCLUDED.follower_count, xiaohongshu_authors.follower_count),
                            following_count = COALESCE(EXCLUDED.following_count, xiaohongshu_authors.following_count),
                            note_count = COALESCE(EXCLUDED.note_count, xiaohongshu_authors.note_count),
                            verified = EXCLUDED.verified,
                            updated_at = EXCLUDED.updated_at,
                            discovered_from_registrations = CASE
                                WHEN :discovered_from IS NOT NULL
                                    AND NOT (:discovered_from = ANY(xiaohongshu_authors.discovered_from_registrations))
                                THEN array_append(xiaohongshu_authors.discovered_from_registrations, :discovered_from)
                                ELSE xiaohongshu_authors.discovered_from_registrations
                            END
                    """),
                    {
                        "user_id": author.user_id,
                        "red_id": author.red_id,
                        "nickname": author.nickname,
                        "avatar_url": author.avatar_url,
                        "description": author.description,
                        "follower_count": author.follower_count,
                        "following_count": author.following_count,
                        "note_count": author.note_count,
                        "verified": author.verified,
                        "scraped_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                        "discovered_from": discovered_from,
                    },
                )
                conn.commit()
                logger.debug(f"Saved author {author.user_id} to database")
                return True

        except SQLAlchemyError as e:
            logger.error(f"Failed to save author {author.user_id} to database: {e}")
            return False

    def update_author_note_count(self, user_id: str) -> bool:
        """Update author's note_count from actual scraped notes in database.

        Args:
            user_id: Author's user ID.

        Returns:
            True if updated successfully, False otherwise.
        """
        if not self.db_engine:
            return False

        try:
            with self.db_engine.connect() as conn:
                # Count notes for this author
                result = conn.execute(
                    text("""
                        UPDATE xiaohongshu_authors
                        SET note_count = (
                            SELECT COUNT(*) FROM xiaohongshu_notes
                            WHERE author_id = :user_id
                        ),
                        updated_at = :updated_at
                        WHERE user_id = :user_id
                        RETURNING note_count
                    """),
                    {
                        "user_id": user_id,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
                row = result.fetchone()
                conn.commit()
                if row:
                    logger.debug(
                        f"Updated author {user_id} note_count to {row[0]}"
                    )
                return True

        except SQLAlchemyError as e:
            logger.error(f"Failed to update author {user_id} note_count: {e}")
            return False

    def save_note(self, note: XiaohongshuNote) -> bool:
        """Save a single note to database immediately after extraction.

        Args:
            note: The note to save.

        Returns:
            True if saved successfully, False otherwise.
        """
        if not self.db_engine:
            return False

        try:
            with self.db_engine.connect() as conn:
                conn.execute(
                    text("""
                        INSERT INTO xiaohongshu_notes (
                            note_id, source_url, title, content, tags, location,
                            author_id, author_name, image_urls, image_paths,
                            video_url, like_count, collect_count, comment_count,
                            comments, note_created_at, scraped_at, updated_at,
                            source_html_path
                        ) VALUES (
                            :note_id, :source_url, :title, :content, :tags, :location,
                            :author_id, :author_name, :image_urls, :image_paths,
                            :video_url, :like_count, :collect_count, :comment_count,
                            :comments, :note_created_at, :scraped_at, :updated_at,
                            :source_html_path
                        )
                        ON CONFLICT (note_id) DO UPDATE SET
                            title = COALESCE(EXCLUDED.title, xiaohongshu_notes.title),
                            content = COALESCE(EXCLUDED.content, xiaohongshu_notes.content),
                            tags = COALESCE(EXCLUDED.tags, xiaohongshu_notes.tags),
                            author_name = COALESCE(EXCLUDED.author_name, xiaohongshu_notes.author_name),
                            image_urls = COALESCE(EXCLUDED.image_urls, xiaohongshu_notes.image_urls),
                            image_paths = COALESCE(EXCLUDED.image_paths, xiaohongshu_notes.image_paths),
                            like_count = COALESCE(EXCLUDED.like_count, xiaohongshu_notes.like_count),
                            collect_count = COALESCE(EXCLUDED.collect_count, xiaohongshu_notes.collect_count),
                            comment_count = COALESCE(EXCLUDED.comment_count, xiaohongshu_notes.comment_count),
                            comments = COALESCE(EXCLUDED.comments, xiaohongshu_notes.comments),
                            note_created_at = COALESCE(EXCLUDED.note_created_at, xiaohongshu_notes.note_created_at),
                            source_html_path = COALESCE(EXCLUDED.source_html_path, xiaohongshu_notes.source_html_path),
                            updated_at = EXCLUDED.updated_at
                    """),
                    {
                        "note_id": note.note_id,
                        "source_url": note.source_url,
                        "title": note.title,
                        "content": note.content,
                        "tags": json.dumps(note.tags) if note.tags else None,
                        "location": note.location,
                        "author_id": note.author_id,
                        "author_name": note.author_name,
                        "image_urls": json.dumps(note.image_urls) if note.image_urls else None,
                        "image_paths": json.dumps(note.image_paths) if note.image_paths else None,
                        "video_url": note.video_url,
                        "like_count": note.like_count,
                        "collect_count": note.collect_count,
                        "comment_count": note.comment_count,
                        "comments": json.dumps([c.model_dump() for c in note.comments]) if note.comments else None,
                        "note_created_at": note.created_at,
                        "scraped_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                        "source_html_path": note.source_html_path,
                    },
                )
                conn.commit()
                logger.debug(f"Saved note {note.note_id} to database")
                return True

        except SQLAlchemyError as e:
            logger.error(f"Failed to save note {note.note_id} to database: {e}")
            return False

    def save_notes_batch(
        self,
        author: XiaohongshuAuthor | None,
        notes: list[XiaohongshuNote],
    ) -> None:
        """Save scraped data to database in a single transaction.

        Saves the author profile and all notes in one batch commit.

        Args:
            author: Author profile to save.
            notes: List of notes to save.
        """
        if not self.db_engine:
            return

        try:
            with self.db_engine.connect() as conn:
                # Save author
                if author:
                    conn.execute(
                        text("""
                            INSERT INTO xiaohongshu_authors (
                                user_id, red_id, nickname, avatar_url, description,
                                follower_count, following_count, note_count, verified,
                                scraped_at, updated_at
                            ) VALUES (
                                :user_id, :red_id, :nickname, :avatar_url, :description,
                                :follower_count, :following_count, :note_count, :verified,
                                :scraped_at, :updated_at
                            )
                            ON CONFLICT (user_id) DO UPDATE SET
                                red_id = COALESCE(EXCLUDED.red_id, xiaohongshu_authors.red_id),
                                nickname = COALESCE(EXCLUDED.nickname, xiaohongshu_authors.nickname),
                                avatar_url = COALESCE(EXCLUDED.avatar_url, xiaohongshu_authors.avatar_url),
                                description = COALESCE(EXCLUDED.description, xiaohongshu_authors.description),
                                follower_count = COALESCE(EXCLUDED.follower_count, xiaohongshu_authors.follower_count),
                                following_count = COALESCE(EXCLUDED.following_count, xiaohongshu_authors.following_count),
                                note_count = COALESCE(EXCLUDED.note_count, xiaohongshu_authors.note_count),
                                verified = EXCLUDED.verified,
                                updated_at = EXCLUDED.updated_at
                        """),
                        {
                            "user_id": author.user_id,
                            "red_id": author.red_id,
                            "nickname": author.nickname,
                            "avatar_url": author.avatar_url,
                            "description": author.description,
                            "follower_count": author.follower_count,
                            "following_count": author.following_count,
                            "note_count": author.note_count,
                            "verified": author.verified,
                            "scraped_at": datetime.now(timezone.utc),
                            "updated_at": datetime.now(timezone.utc),
                        },
                    )

                # Save notes
                for note in notes:
                    conn.execute(
                        text("""
                            INSERT INTO xiaohongshu_notes (
                                note_id, source_url, title, content, tags, location,
                                author_id, author_name, image_urls, image_paths,
                                video_url, like_count, collect_count, comment_count,
                                comments, note_created_at, scraped_at, updated_at,
                                source_html_path
                            ) VALUES (
                                :note_id, :source_url, :title, :content, :tags, :location,
                                :author_id, :author_name, :image_urls, :image_paths,
                                :video_url, :like_count, :collect_count, :comment_count,
                                :comments, :note_created_at, :scraped_at, :updated_at,
                                :source_html_path
                            )
                            ON CONFLICT (note_id) DO UPDATE SET
                                title = COALESCE(EXCLUDED.title, xiaohongshu_notes.title),
                                content = COALESCE(EXCLUDED.content, xiaohongshu_notes.content),
                                tags = COALESCE(EXCLUDED.tags, xiaohongshu_notes.tags),
                                author_name = COALESCE(EXCLUDED.author_name, xiaohongshu_notes.author_name),
                                image_urls = COALESCE(EXCLUDED.image_urls, xiaohongshu_notes.image_urls),
                                image_paths = COALESCE(EXCLUDED.image_paths, xiaohongshu_notes.image_paths),
                                like_count = COALESCE(EXCLUDED.like_count, xiaohongshu_notes.like_count),
                                collect_count = COALESCE(EXCLUDED.collect_count, xiaohongshu_notes.collect_count),
                                comment_count = COALESCE(EXCLUDED.comment_count, xiaohongshu_notes.comment_count),
                                comments = COALESCE(EXCLUDED.comments, xiaohongshu_notes.comments),
                                note_created_at = COALESCE(EXCLUDED.note_created_at, xiaohongshu_notes.note_created_at),
                                source_html_path = COALESCE(EXCLUDED.source_html_path, xiaohongshu_notes.source_html_path),
                                updated_at = EXCLUDED.updated_at
                        """),
                        {
                            "note_id": note.note_id,
                            "source_url": note.source_url,
                            "title": note.title,
                            "content": note.content,
                            "tags": json.dumps(note.tags) if note.tags else None,
                            "location": note.location,
                            "author_id": note.author_id,
                            "author_name": note.author_name,
                            "image_urls": json.dumps(note.image_urls) if note.image_urls else None,
                            "image_paths": json.dumps(note.image_paths) if note.image_paths else None,
                            "video_url": note.video_url,
                            "like_count": note.like_count,
                            "collect_count": note.collect_count,
                            "comment_count": note.comment_count,
                            "comments": json.dumps([c.model_dump() for c in note.comments]) if note.comments else None,
                            "note_created_at": note.created_at,
                            "scraped_at": datetime.now(timezone.utc),
                            "updated_at": datetime.now(timezone.utc),
                            "source_html_path": note.source_html_path,
                        },
                    )

                conn.commit()
                logger.info(f"Saved {len(notes)} notes to database")

        except SQLAlchemyError as e:
            logger.error(f"Database save failed: {e}")

    def ensure_following_tables_exist(self) -> None:
        """Create database tables for following data if they don't exist."""
        if not self.db_engine:
            return

        try:
            with self.db_engine.connect() as conn:
                # Create xiaohongshu_following table
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS xiaohongshu_following (
                        id BIGSERIAL PRIMARY KEY,
                        follower_user_id VARCHAR(50) NOT NULL,
                        following_user_id VARCHAR(50) NOT NULL,
                        following_nickname VARCHAR(100),
                        following_avatar_url VARCHAR(500),
                        following_red_id VARCHAR(50),
                        following_description TEXT,
                        following_verified BOOLEAN DEFAULT FALSE,
                        following_follower_count INTEGER,
                        following_note_count INTEGER,
                        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(follower_user_id, following_user_id)
                    )
                """))

                # Create indexes
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_xhs_following_follower
                    ON xiaohongshu_following(follower_user_id)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_xhs_following_following
                    ON xiaohongshu_following(following_user_id)
                """))

                conn.commit()
                logger.info("Xiaohongshu following tables ensured")

        except SQLAlchemyError as e:
            logger.error(f"Failed to create tables: {e}")

    def save_following(
        self,
        follower_id: str,
        following: XiaohongshuFollowing,
    ) -> bool:
        """Save a following relationship to database.

        Also syncs the followed user to xiaohongshu_authors table.

        Args:
            follower_id: The user ID of the follower.
            following: The followed user information.

        Returns:
            True if saved successfully, False otherwise.
        """
        if not self.db_engine:
            return False

        try:
            with self.db_engine.connect() as conn:
                # Save to xiaohongshu_following table
                conn.execute(
                    text("""
                        INSERT INTO xiaohongshu_following (
                            follower_user_id, following_user_id,
                            following_nickname, following_avatar_url,
                            following_red_id, following_description,
                            following_verified, following_follower_count,
                            following_note_count, scraped_at, updated_at
                        ) VALUES (
                            :follower_user_id, :following_user_id,
                            :following_nickname, :following_avatar_url,
                            :following_red_id, :following_description,
                            :following_verified, :following_follower_count,
                            :following_note_count, :scraped_at, :updated_at
                        )
                        ON CONFLICT (follower_user_id, following_user_id) DO UPDATE SET
                            following_nickname = COALESCE(EXCLUDED.following_nickname, xiaohongshu_following.following_nickname),
                            following_avatar_url = COALESCE(EXCLUDED.following_avatar_url, xiaohongshu_following.following_avatar_url),
                            following_red_id = COALESCE(EXCLUDED.following_red_id, xiaohongshu_following.following_red_id),
                            following_description = COALESCE(EXCLUDED.following_description, xiaohongshu_following.following_description),
                            following_verified = EXCLUDED.following_verified,
                            following_follower_count = COALESCE(EXCLUDED.following_follower_count, xiaohongshu_following.following_follower_count),
                            following_note_count = COALESCE(EXCLUDED.following_note_count, xiaohongshu_following.following_note_count),
                            updated_at = EXCLUDED.updated_at
                    """),
                    {
                        "follower_user_id": follower_id,
                        "following_user_id": following.user_id,
                        "following_nickname": following.nickname,
                        "following_avatar_url": following.avatar_url,
                        "following_red_id": following.red_id,
                        "following_description": following.description,
                        "following_verified": following.verified,
                        "following_follower_count": following.follower_count,
                        "following_note_count": following.note_count,
                        "scraped_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    },
                )

                # Also save to xiaohongshu_authors table
                conn.execute(
                    text("""
                        INSERT INTO xiaohongshu_authors (
                            user_id, red_id, nickname, avatar_url, description,
                            follower_count, note_count, verified,
                            scraped_at, updated_at
                        ) VALUES (
                            :user_id, :red_id, :nickname, :avatar_url, :description,
                            :follower_count, :note_count, :verified,
                            :scraped_at, :updated_at
                        )
                        ON CONFLICT (user_id) DO UPDATE SET
                            red_id = COALESCE(EXCLUDED.red_id, xiaohongshu_authors.red_id),
                            nickname = COALESCE(EXCLUDED.nickname, xiaohongshu_authors.nickname),
                            avatar_url = COALESCE(EXCLUDED.avatar_url, xiaohongshu_authors.avatar_url),
                            description = COALESCE(EXCLUDED.description, xiaohongshu_authors.description),
                            follower_count = COALESCE(EXCLUDED.follower_count, xiaohongshu_authors.follower_count),
                            note_count = COALESCE(EXCLUDED.note_count, xiaohongshu_authors.note_count),
                            verified = EXCLUDED.verified,
                            updated_at = EXCLUDED.updated_at
                    """),
                    {
                        "user_id": following.user_id,
                        "red_id": following.red_id,
                        "nickname": following.nickname,
                        "avatar_url": following.avatar_url,
                        "description": following.description,
                        "follower_count": following.follower_count,
                        "note_count": following.note_count,
                        "verified": following.verified,
                        "scraped_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    },
                )

                conn.commit()
                return True

        except SQLAlchemyError as e:
            logger.error(f"Failed to save following {following.user_id}: {e}")
            return False


# =============================================================================
# Utility Functions
# =============================================================================


def get_notes_without_images(database_url: str) -> list[dict[str, Any]]:
    """Get all notes that don't have downloaded images.

    Args:
        database_url: Database connection URL.

    Returns:
        List of note dictionaries without images.
    """
    engine = create_engine(database_url, echo=False, pool_pre_ping=True)

    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT note_id, author_id, title, image_urls, image_paths
                FROM xiaohongshu_notes
                WHERE image_paths IS NULL
                   OR image_paths = '[]'::jsonb
                   OR jsonb_array_length(image_paths) = 0
                ORDER BY scraped_at DESC
            """))

            notes = []
            for row in result:
                notes.append({
                    "note_id": row[0],
                    "author_id": row[1],
                    "title": row[2],
                    "image_urls": row[3],
                    "image_paths": row[4],
                })
            return notes

    except SQLAlchemyError as e:
        logger.error(f"Failed to get notes without images: {e}")
        return []


def get_notes_stats(database_url: str) -> dict[str, Any]:
    """Get statistics about notes and their image status.

    Args:
        database_url: Database connection URL.

    Returns:
        Statistics dictionary.
    """
    engine = create_engine(database_url, echo=False, pool_pre_ping=True)

    try:
        with engine.connect() as conn:
            # Total notes
            total = conn.execute(
                text("SELECT COUNT(*) FROM xiaohongshu_notes")
            ).scalar()

            # Notes with images
            with_images = conn.execute(text("""
                SELECT COUNT(*) FROM xiaohongshu_notes
                WHERE image_paths IS NOT NULL
                  AND image_paths != '[]'::jsonb
                  AND jsonb_array_length(image_paths) > 0
            """)).scalar()

            # Notes without images but with image_urls
            has_urls_no_images = conn.execute(text("""
                SELECT COUNT(*) FROM xiaohongshu_notes
                WHERE (image_paths IS NULL OR image_paths = '[]'::jsonb)
                  AND image_urls IS NOT NULL
                  AND image_urls != '[]'::jsonb
                  AND jsonb_array_length(image_urls) > 0
            """)).scalar()

            # Notes with S3 URLs
            with_s3 = conn.execute(text("""
                SELECT COUNT(*) FROM xiaohongshu_notes
                WHERE image_paths IS NOT NULL
                  AND image_paths::text LIKE '%s3.amazonaws.com%'
            """)).scalar()

            return {
                "total_notes": total or 0,
                "notes_with_images": with_images or 0,
                "notes_without_images": (total or 0) - (with_images or 0),
                "notes_with_urls_no_images": has_urls_no_images or 0,
                "notes_with_s3_images": with_s3 or 0,
            }

    except SQLAlchemyError as e:
        logger.error(f"Failed to get notes stats: {e}")
        return {}


def reset_notes_for_rescrape(
    database_url: str,
    author_id: str | None = None,
    dry_run: bool = True,
) -> int:
    """Reset notes without images to allow re-scraping.

    This clears the image_paths field for notes that don't have images,
    ensuring they will be re-scraped when the author is processed again.

    Args:
        database_url: Database connection URL.
        author_id: Optional author ID to limit reset scope.
        dry_run: If True, only report what would be reset.

    Returns:
        Number of notes that would be/were reset.
    """
    engine = create_engine(database_url, echo=False, pool_pre_ping=True)

    try:
        with engine.connect() as conn:
            # Build query
            if author_id:
                count_query = text("""
                    SELECT COUNT(*) FROM xiaohongshu_notes
                    WHERE author_id = :author_id
                      AND (image_paths IS NULL OR image_paths = '[]'::jsonb)
                """)
                update_query = text("""
                    UPDATE xiaohongshu_notes
                    SET image_paths = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE author_id = :author_id
                      AND (image_paths IS NULL OR image_paths = '[]'::jsonb)
                """)
                params: dict[str, Any] = {"author_id": author_id}
            else:
                count_query = text("""
                    SELECT COUNT(*) FROM xiaohongshu_notes
                    WHERE image_paths IS NULL OR image_paths = '[]'::jsonb
                """)
                update_query = text("""
                    UPDATE xiaohongshu_notes
                    SET image_paths = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE image_paths IS NULL OR image_paths = '[]'::jsonb
                """)
                params = {}

            # Count affected notes
            count = conn.execute(count_query, params).scalar() or 0

            if dry_run:
                logger.info(f"Would reset {count} notes for re-scraping (dry run)")
            else:
                conn.execute(update_query, params)
                conn.commit()
                logger.info(f"Reset {count} notes for re-scraping")

            return count

    except SQLAlchemyError as e:
        logger.error(f"Failed to reset notes: {e}")
        return 0
