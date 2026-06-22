from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import relationship
from app.database import Base


class Series(Base):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True, index=True)
    source = Column(String, nullable=False, default="manual")
    created_at = Column(DateTime, default=datetime.utcnow)

    books = relationship("Book", back_populates="series")


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    isbn = Column(String, nullable=True, index=True)
    title = Column(String, nullable=False)
    subtitle = Column(String, nullable=True)
    authors = Column(Text, nullable=True)
    publisher = Column(String, nullable=True)
    publish_date = Column(String, nullable=True)
    cover_url = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    page_count = Column(Integer, nullable=True)
    language = Column(String, nullable=True)
    source = Column(String, nullable=False, default="manual")
    work_key = Column(String, nullable=True)

    series_id = Column(Integer, ForeignKey("series.id"), nullable=True)
    series_position = Column(Float, nullable=True)

    shelf = Column(String, nullable=True)
    location_id = Column(Integer, ForeignKey("shelves.id"), nullable=True)   # legacy shelf
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    enrichment_status = Column(String, nullable=False, default="ok")
    enrichment_source = Column(String, nullable=True)  # source principale (ex: "sudoc", "bnf")
    source_data = Column(Text, nullable=True)  # JSON: résultats bruts par source

    series = relationship("Series", back_populates="books")
    location = relationship("Shelf", back_populates="books", foreign_keys=[location_id])
    room = relationship("Room", foreign_keys=[room_id])
    loans = relationship("Loan", back_populates="book")


class Site(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    rooms = relationship("Room", back_populates="site", cascade="all, delete-orphan")


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=True)

    site = relationship("Site", back_populates="rooms")
    shelves = relationship("Shelf", back_populates="room", cascade="all, delete-orphan")


class Shelf(Base):
    __tablename__ = "shelves"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)

    room = relationship("Room", back_populates="shelves")
    books = relationship("Book", back_populates="location", foreign_keys="[Book.location_id]")

    @property
    def label(self) -> str:
        parts = []
        if self.room and self.room.site:
            parts.append(self.room.site.name)
        if self.room:
            parts.append(self.room.name)
        parts.append(self.name)
        return " · ".join(p for p in parts if p) or "Sans localisation"


class Borrower(Base):
    __tablename__ = "borrowers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    contact = Column(String, nullable=True)
    notes = Column(Text, nullable=True)

    loans = relationship("Loan", back_populates="borrower")


class Loan(Base):
    __tablename__ = "loans"

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=False)
    borrower_id = Column(Integer, ForeignKey("borrowers.id"), nullable=True)   # emprunteur externe
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)           # ou compte du site
    loan_date = Column(DateTime, nullable=False, default=datetime.utcnow)
    due_date = Column(DateTime, nullable=True)
    return_date = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    book = relationship("Book", back_populates="loans")
    borrower = relationship("Borrower", back_populates="loans")
    user = relationship("User", foreign_keys=[user_id])


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id         = Column(Integer, primary_key=True, index=True)
    book_id    = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    action     = Column(String, nullable=False)   # created | updated | enriched | relocated | deleted
    detail     = Column(Text, nullable=True)       # JSON des champs modifiés
    created_at = Column(DateTime, default=datetime.utcnow)

    book = relationship("Book")
    user = relationship("User", foreign_keys=[user_id])


class SeriesMissingVolume(Base):
    __tablename__ = "series_missing_volumes"

    id         = Column(Integer, primary_key=True, index=True)
    series_id  = Column(Integer, ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True)
    position   = Column(Float, nullable=True)
    title      = Column(String, nullable=True)   # titre connu via DDG
    detected_at = Column(DateTime, default=datetime.utcnow)

    series = relationship("Series")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="contributeur")
    is_active = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    email = Column(String, nullable=True)
    avatar = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AppLog(Base):
    __tablename__ = "app_logs"

    id         = Column(Integer, primary_key=True, index=True)
    level      = Column(String, nullable=False, default="info")     # info | warning | error
    category   = Column(String, nullable=False, default="system")   # task | scan | settings | system
    message    = Column(String, nullable=False)
    detail     = Column(Text, nullable=True)   # JSON optionnel
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
