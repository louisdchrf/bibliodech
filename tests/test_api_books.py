"""
Tests d'intégration pour l'API /api/books.
Utilise TestClient FastAPI avec une DB SQLite de test.
"""
import json
import pytest
from datetime import datetime
from app.models import Book, User
from app.auth import hash_password


def _create_admin(db):
    user = User(
        username="admin_api_test",
        password_hash=hash_password("pass"),
        role="admin",
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    return user


def _auth_headers(client, db):
    """Retourne les cookies de session après login."""
    _create_admin(db)
    db.commit()
    r = client.post("/login", data={"username": "admin_api_test", "password": "pass"}, follow_redirects=False)
    return {}  # le cookie est géré automatiquement par TestClient


def _make_book(db, **kwargs):
    book = Book(
        title=kwargs.get("title", "Livre test"),
        authors=json.dumps(kwargs.get("authors", ["Auteur"])),
        isbn=kwargs.get("isbn", None),
        source=kwargs.get("source", "manual"),
        enrichment_status=kwargs.get("enrichment_status", "ok"),
        room_id=kwargs.get("room_id", None),
        series_id=kwargs.get("series_id", None),
        cover_url=kwargs.get("cover_url", None),
        added_at=datetime.utcnow(),
    )
    db.add(book)
    db.flush()
    return book


class TestListBooks:
    def test_requires_auth(self, client):
        r = client.get("/api/books")
        assert r.status_code == 401

    def test_returns_books_when_authenticated(self, client, db):
        _auth_headers(client, db)
        _make_book(db, title="Mon livre")
        db.commit()
        r = client.get("/api/books")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert data["total"] >= 1

    def test_search_by_title(self, client, db):
        _auth_headers(client, db)
        _make_book(db, title="Unique XYZ12345")
        _make_book(db, title="Autre livre")
        db.commit()
        r = client.get("/api/books?search=XYZ12345")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert "XYZ12345" in data["items"][0]["title"]

    def test_filter_no_cover(self, client, db):
        _auth_headers(client, db)
        _make_book(db, title="Avec couverture", cover_url="/covers/test.jpg")
        _make_book(db, title="Sans couverture", cover_url=None)
        db.commit()
        r = client.get("/api/books?has_cover=no")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(item["cover_url"] is None for item in items)

    def test_filter_with_cover(self, client, db):
        _auth_headers(client, db)
        _make_book(db, title="Avec couv", cover_url="/covers/test2.jpg")
        _make_book(db, title="Sans couv")
        db.commit()
        r = client.get("/api/books?has_cover=yes")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(item["cover_url"] is not None for item in items)

    def test_filter_no_series(self, client, db):
        _auth_headers(client, db)
        _make_book(db, title="Sans série", series_id=None)
        db.commit()
        r = client.get("/api/books?series_id=none")
        assert r.status_code == 200
        items = r.json()["items"]
        # Tous les livres retournés n'ont pas de série
        # (On vérifie juste que le filtre est accepté sans erreur)
        assert isinstance(items, list)

    def test_pagination(self, client, db):
        _auth_headers(client, db)
        for i in range(5):
            _make_book(db, title=f"Livre paginate {i}")
        db.commit()
        r = client.get("/api/books?limit=2&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) <= 2
        assert data["total"] >= 5

    def test_sort_by_title(self, client, db):
        _auth_headers(client, db)
        _make_book(db, title="Zebra")
        _make_book(db, title="Alpha")
        db.commit()
        r = client.get("/api/books?sort_by=title")
        assert r.status_code == 200
        titles = [item["title"] for item in r.json()["items"]]
        assert titles == sorted(titles)


class TestGetBook:
    def test_get_existing_book(self, client, db):
        _auth_headers(client, db)
        book = _make_book(db, title="Livre précis")
        db.commit()
        r = client.get(f"/api/books/{book.id}")
        assert r.status_code == 200
        assert r.json()["title"] == "Livre précis"

    def test_get_nonexistent_book(self, client, db):
        _auth_headers(client, db)
        r = client.get("/api/books/999999")
        assert r.status_code == 404
