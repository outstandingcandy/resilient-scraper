"""FastAPI router for Xiaohongshu data query endpoints."""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from resilient_scraper.service.database import Database

logger = logging.getLogger("resilient_scraper.scrapers.xiaohongshu.router")


# --- Response Models ---


class AuthorResponse(BaseModel):
    """Xiaohongshu author."""

    user_id: str
    red_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    note_count: int | None = None
    verified: bool = False
    scraped_at: datetime | None = None


class NoteResponse(BaseModel):
    """Xiaohongshu note."""

    note_id: str
    title: str | None = None
    content: str | None = None
    author_id: str
    author_name: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    image_paths: list[str] = Field(default_factory=list)
    video_url: str | None = None
    like_count: int | None = None
    collect_count: int | None = None
    comment_count: int | None = None
    tags: list[str] = Field(default_factory=list)
    location: str | None = None
    source_url: str | None = None
    note_created_at: datetime | None = None
    scraped_at: datetime | None = None
    updated_at: datetime | None = None


def create_router(db: Database) -> APIRouter:
    """Create the Xiaohongshu data query router."""
    router = APIRouter()

    @router.get("/authors", response_model=list[AuthorResponse])
    async def list_authors(
        search: str | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> list[AuthorResponse]:
        """List authors with optional search."""
        conditions = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if search:
            conditions.append("(nickname ILIKE :search OR user_id ILIKE :search OR red_id ILIKE :search)")
            params["search"] = f"%{search}%"

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT user_id, red_id, nickname, avatar_url, description,
                   follower_count, following_count, note_count, verified, scraped_at
            FROM xiaohongshu_authors {where}
            ORDER BY scraped_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """

        async with db.session() as session:
            result = await session.execute(text(query), params)
            rows = result.mappings().fetchall()
            return [AuthorResponse(**dict(row)) for row in rows]

    @router.get("/authors/{user_id}", response_model=AuthorResponse)
    async def get_author(user_id: str) -> AuthorResponse:
        """Get author details."""
        async with db.session() as session:
            result = await session.execute(
                text("""
                    SELECT user_id, red_id, nickname, avatar_url, description,
                           follower_count, following_count, note_count, verified, scraped_at
                    FROM xiaohongshu_authors WHERE user_id = :user_id
                """),
                {"user_id": user_id},
            )
            row = result.mappings().fetchone()
            if not row:
                raise HTTPException(404, "Author not found")
            return AuthorResponse(**dict(row))

    @router.get("/authors/{user_id}/notes", response_model=list[NoteResponse])
    async def list_author_notes(
        user_id: str,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> list[NoteResponse]:
        """List notes by a specific author."""
        async with db.session() as session:
            result = await session.execute(
                text("""
                    SELECT note_id, title, content, author_id, author_name,
                           image_urls, image_paths, video_url,
                           like_count, collect_count, comment_count,
                           tags, location, source_url, note_created_at, scraped_at, updated_at
                    FROM xiaohongshu_notes
                    WHERE author_id = :user_id
                    ORDER BY COALESCE(updated_at, scraped_at) DESC NULLS LAST
                    LIMIT :limit OFFSET :offset
                """),
                {"user_id": user_id, "limit": limit, "offset": offset},
            )
            rows = result.mappings().fetchall()
            return [_note_from_row(row) for row in rows]

    @router.get("/notes/{note_id}", response_model=NoteResponse)
    async def get_note(note_id: str) -> NoteResponse:
        """Get note details."""
        async with db.session() as session:
            result = await session.execute(
                text("""
                    SELECT note_id, title, content, author_id, author_name,
                           image_urls, image_paths, video_url,
                           like_count, collect_count, comment_count,
                           tags, location, source_url, note_created_at, scraped_at, updated_at
                    FROM xiaohongshu_notes WHERE note_id = :note_id
                """),
                {"note_id": note_id},
            )
            row = result.mappings().fetchone()
            if not row:
                raise HTTPException(404, "Note not found")
            return _note_from_row(row)

    @router.get("/notes", response_model=list[NoteResponse])
    async def search_notes(
        keyword: str | None = None,
        author_id: str | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> list[NoteResponse]:
        """Search notes by keyword or author."""
        conditions = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if keyword:
            conditions.append("(title ILIKE :keyword OR content ILIKE :keyword)")
            params["keyword"] = f"%{keyword}%"
        if author_id:
            conditions.append("author_id = :author_id")
            params["author_id"] = author_id

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT note_id, title, content, author_id, author_name,
                   image_urls, image_paths, video_url,
                   like_count, collect_count, comment_count,
                   tags, location, source_url, note_created_at, scraped_at, updated_at
            FROM xiaohongshu_notes {where}
            ORDER BY COALESCE(updated_at, scraped_at) DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """

        async with db.session() as session:
            result = await session.execute(text(query), params)
            rows = result.mappings().fetchall()
            return [_note_from_row(row) for row in rows]

    return router


def _note_from_row(row: Any) -> NoteResponse:
    """Convert a database row to NoteResponse, handling JSONB fields."""
    data = dict(row)
    # JSONB fields come back as Python objects, ensure they're lists
    for field_name in ("image_urls", "image_paths", "tags"):
        val = data.get(field_name)
        if val is None:
            data[field_name] = []
        elif isinstance(val, str):
            import json
            try:
                data[field_name] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                data[field_name] = []
    return NoteResponse(**data)
