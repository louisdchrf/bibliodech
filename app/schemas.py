from __future__ import annotations
import json
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator


# ── Series ──────────────────────────────────────────────────────────────────

class SeriesBase(BaseModel):
    name: str
    source: str = "manual"


class SeriesCreate(SeriesBase):
    pass


class SeriesOut(SeriesBase):
    id: int
    created_at: datetime
    book_count: int = 0

    model_config = {"from_attributes": True}


# ── Book ─────────────────────────────────────────────────────────────────────

class BookBase(BaseModel):
    isbn: Optional[str] = None
    title: str
    subtitle: Optional[str] = None
    authors: Optional[list[str]] = None
    publisher: Optional[str] = None
    publish_date: Optional[str] = None
    cover_url: Optional[str] = None
    description: Optional[str] = None
    page_count: Optional[int] = None
    language: Optional[str] = None
    source: str = "manual"
    work_key: Optional[str] = None
    series_id: Optional[int] = None
    series_position: Optional[float] = None
    shelf: Optional[str] = None
    location_id: Optional[int] = None


class BookCreate(BookBase):
    pass


class BookUpdate(BaseModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    authors: Optional[list[str]] = None
    publisher: Optional[str] = None
    publish_date: Optional[str] = None
    cover_url: Optional[str] = None
    description: Optional[str] = None
    page_count: Optional[int] = None
    language: Optional[str] = None
    series_id: Optional[int] = None
    series_position: Optional[float] = None
    shelf: Optional[str] = None
    location_id: Optional[int] = None
    room_id: Optional[int] = None
    enrichment_status: Optional[str] = None
    genre: Optional[str] = None


class BookOut(BookBase):
    id: int
    added_at: datetime
    series_name: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("authors", mode="before")
    @classmethod
    def parse_authors(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return [v]
        return v


# ── User ─────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "contributeur"
    must_change_password: bool = False
    email: Optional[str] = None


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    must_change_password: Optional[bool] = None
    email: Optional[str] = None
    default_room_id: Optional[int] = None


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    must_change_password: bool = False
    email: Optional[str] = None
    created_at: datetime
    default_room_id: Optional[int] = None

    model_config = {"from_attributes": True}


# ── Scan ─────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    isbn: str
    shelf: Optional[str] = None        # legacy
    location_id: Optional[int] = None  # legacy shelf
    room_id: Optional[int] = None


# ── Series link ──────────────────────────────────────────────────────────────

class LinkBooksRequest(BaseModel):
    book_ids: list[int]
    series_name: str


# ── Bulk actions ──────────────────────────────────────────────────────────────

class BulkUpdateData(BaseModel):
    authors: Optional[list[str]] = None
    series_id: Optional[int] = None
    series_position: Optional[float] = None
    series_start: Optional[int] = None   # numéro de tome de départ (incrémenté par livre)
    room_id: Optional[int] = None


class BulkActionRequest(BaseModel):
    ids: list[int]
    action: str                              # "delete" | "update"
    data: Optional[BulkUpdateData] = None
