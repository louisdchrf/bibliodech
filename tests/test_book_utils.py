"""
Tests pour book_to_dict — sérialisation des livres.
"""
import json
import pytest
from datetime import datetime
from unittest.mock import MagicMock

from app.book_utils import book_to_dict, _safe_json, utc_iso


# ── _safe_json ───────────────────────────────────────────────────────────────

class TestSafeJson:
    def test_valid_list(self):
        assert _safe_json('["Alice", "Bob"]', [], 1, "authors") == ["Alice", "Bob"]

    def test_valid_dict(self):
        assert _safe_json('{"key": "val"}', None, 1, "source_data") == {"key": "val"}

    def test_none_returns_default(self):
        assert _safe_json(None, [], 1, "authors") == []
        assert _safe_json(None, None, 1, "source_data") is None

    def test_empty_string_returns_default(self):
        assert _safe_json("", [], 1, "authors") == []

    def test_invalid_json_returns_default(self):
        assert _safe_json("{bad json", [], 1, "authors") == []
        assert _safe_json("{bad json", None, 1, "source_data") is None


# ── utc_iso ──────────────────────────────────────────────────────────────────

class TestUtcIso:
    def test_none_returns_none(self):
        assert utc_iso(None) is None

    def test_naive_datetime_gets_z_suffix(self):
        dt = datetime(2024, 1, 15, 12, 0, 0)
        result = utc_iso(dt)
        assert result.endswith("Z")
        assert "2024-01-15" in result

    def test_already_has_timezone(self):
        from datetime import timezone, timedelta
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        result = utc_iso(dt)
        assert "+" in result  # garde le timezone original


# ── book_to_dict ─────────────────────────────────────────────────────────────

def _make_book(**kwargs):
    """Crée un mock Book avec des valeurs par défaut."""
    book = MagicMock()
    book.id = kwargs.get("id", 1)
    book.isbn = kwargs.get("isbn", "9782012345678")
    book.title = kwargs.get("title", "Livre de test")
    book.subtitle = kwargs.get("subtitle", None)
    book.authors = kwargs.get("authors", '["Auteur Test"]')
    book.publisher = kwargs.get("publisher", "Éditeur Test")
    book.publish_date = kwargs.get("publish_date", "2020")
    book.cover_url = kwargs.get("cover_url", None)
    book.description = kwargs.get("description", None)
    book.page_count = kwargs.get("page_count", 200)
    book.language = kwargs.get("language", "fr")
    book.source = kwargs.get("source", "manual")
    book.work_key = kwargs.get("work_key", None)
    book.room = kwargs.get("room", None)
    book.location = kwargs.get("location", None)
    book.added_at = kwargs.get("added_at", datetime(2024, 1, 1))
    book.enrichment_status = kwargs.get("enrichment_status", "ok")
    book.loans = kwargs.get("loans", [])
    book.source_data = kwargs.get("source_data", None)
    return book


class TestBookToDict:
    def test_basic_fields(self):
        book = _make_book()
        result = book_to_dict(book)
        assert result["id"] == 1
        assert result["title"] == "Livre de test"
        assert result["authors"] == ["Auteur Test"]
        assert result["enrichment_status"] == "ok"

    def test_authors_parsed_from_json(self):
        book = _make_book(authors='["Hugo", "Zola"]')
        result = book_to_dict(book)
        assert result["authors"] == ["Hugo", "Zola"]

    def test_authors_empty_when_none(self):
        book = _make_book(authors=None)
        result = book_to_dict(book)
        assert result["authors"] == []

    def test_authors_invalid_json_returns_empty(self):
        book = _make_book(authors="{invalid}")
        result = book_to_dict(book)
        assert result["authors"] == []

    def test_source_data_parsed(self):
        book = _make_book(source_data='{"sudoc": {"title": "Test"}}')
        result = book_to_dict(book)
        assert result["source_data"] == {"sudoc": {"title": "Test"}}

    def test_source_data_none_when_null(self):
        book = _make_book(source_data=None)
        result = book_to_dict(book)
        assert result["source_data"] is None

    def test_source_data_invalid_json_returns_none(self):
        book = _make_book(source_data="{broken")
        result = book_to_dict(book)
        assert result["source_data"] is None

    def test_location_none_when_no_room(self):
        book = _make_book(room=None, location=None)
        result = book_to_dict(book)
        assert result["location"] is None
        assert result["room_id"] is None

    def test_active_loan_none_when_no_loans(self):
        book = _make_book(loans=[])
        result = book_to_dict(book)
        assert result["active_loan"] is None

    def test_active_loan_detected(self):
        loan = MagicMock()
        loan.return_date = None
        loan.id = 42
        loan.user = None
        loan.user_id = None
        loan.borrower = MagicMock()
        loan.borrower.name = "Jean Dupont"
        loan.loan_date = datetime(2024, 3, 1)
        loan.due_date = None
        book = _make_book(loans=[loan])
        result = book_to_dict(book)
        assert result["active_loan"] is not None
        assert result["active_loan"]["borrower_name"] == "Jean Dupont"

    def test_returned_loan_ignored(self):
        loan = MagicMock()
        loan.return_date = datetime(2024, 4, 1)  # déjà rendu
        book = _make_book(loans=[loan])
        result = book_to_dict(book)
        assert result["active_loan"] is None
