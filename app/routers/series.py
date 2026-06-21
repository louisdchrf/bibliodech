import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin, require_contributor
from app.database import get_db, SessionLocal
from app.models import Book, Series
from app.schemas import LinkBooksRequest
from app.series_logic import get_or_create_series
from app.lookup import _extract_series_and_position

router = APIRouter()


def _series_to_dict(series: Series, book_count: int = 0) -> dict:
    return {
        "id": series.id,
        "name": series.name,
        "source": series.source,
        "created_at": series.created_at.isoformat() if series.created_at else None,
        "book_count": book_count,
    }


def _book_mini(book: Book) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "authors": json.loads(book.authors) if book.authors else [],
        "cover_url": book.cover_url,
        "series_position": book.series_position,
        "shelf": book.shelf,
    }


@router.get("/api/series")
def list_series(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series_list = db.query(Series).order_by(Series.name).all()
    result = []
    for s in series_list:
        count = db.query(Book).filter(Book.series_id == s.id).count()
        result.append(_series_to_dict(s, book_count=count))
    return result


@router.post("/api/series", status_code=201)
def create_series(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom est requis")
    existing = db.query(Series).filter(Series.name == name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Cette série existe déjà")
    s = Series(name=name, source=body.get("source", "manual"))
    db.add(s)
    db.commit()
    db.refresh(s)
    return _series_to_dict(s)


@router.post("/api/series/detect-all")
def detect_series_all(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Lance la détection de série sur tous les livres sans série (heuristiques titre + re-lookup API)."""
    user = get_current_user(request, db)
    require_contributor(user)

    # Passe 1 : heuristiques titre (instantané)
    books_no_series = (
        db.query(Book)
        .filter(Book.series_id.is_(None), Book.enrichment_status == "ok")
        .all()
    )
    found_instant = 0
    queued_lookup = []
    for book in books_no_series:
        title = book.title or ""
        subtitle = book.subtitle
        series_name, series_position = _extract_series_and_position(title, subtitle)
        if series_name:
            series = get_or_create_series(db, series_name, source="manual")
            book.series_id = series.id
            if series_position is not None and book.series_position is None:
                book.series_position = series_position
            found_instant += 1
        else:
            queued_lookup.append(book.id)
    db.commit()

    # Passe 2 : re-lookup API pour les livres restants sans correspondance titre
    if queued_lookup:
        import asyncio
        from app.routers.scan import _enrich_book
        for book_id in queued_lookup:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book and book.isbn:
                book.enrichment_status = "pending"
        db.commit()
        for book_id in queued_lookup:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book and book.isbn:
                background_tasks.add_task(_enrich_book, book_id, book.isbn)

    return {
        "found_instant": found_instant,
        "queued_lookup": len(queued_lookup),
    }


@router.get("/api/series/suggestions")
def series_suggestions(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    # Group books without a series by author
    books_no_series = db.query(Book).filter(Book.series_id == None).all()  # noqa: E711

    author_groups: dict[str, list[dict]] = {}
    for book in books_no_series:
        try:
            authors = json.loads(book.authors) if book.authors else []
        except Exception:
            authors = []
        for author in authors:
            if author not in author_groups:
                author_groups[author] = []
            author_groups[author].append(_book_mini(book))

    # Only return groups with 2+ books
    result = []
    seen_book_ids: set[int] = set()
    for author, books in author_groups.items():
        unique_books = [b for b in books if b["id"] not in seen_book_ids]
        if len(unique_books) >= 2:
            for b in unique_books:
                seen_book_ids.add(b["id"])
            result.append({"author": author, "books": unique_books})

    return result


@router.get("/api/series/{series_id}")
def get_series(series_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404, detail="Série introuvable")
    books = (
        db.query(Book)
        .filter(Book.series_id == series_id)
        .order_by(Book.series_position.nullslast(), Book.title)
        .all()
    )
    data = _series_to_dict(series, book_count=len(books))
    data["books"] = [_book_mini(b) for b in books]
    return data


@router.put("/api/series/{series_id}")
def update_series(
    series_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404, detail="Série introuvable")
    if "name" in body:
        series.name = body["name"]
    db.commit()
    db.refresh(series)
    count = db.query(Book).filter(Book.series_id == series_id).count()
    return _series_to_dict(series, book_count=count)


@router.delete("/api/series/{series_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_series(series_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)

    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404, detail="Série introuvable")
    # Detach books
    db.query(Book).filter(Book.series_id == series_id).update(
        {"series_id": None, "series_position": None}
    )
    db.delete(series)
    db.commit()


@router.post("/api/series/link-books")
def link_books(
    body: LinkBooksRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    series = get_or_create_series(db, body.series_name, source="manual")
    for book_id in body.book_ids:
        book = db.query(Book).filter(Book.id == book_id).first()
        if book:
            book.series_id = series.id
    db.commit()
    count = db.query(Book).filter(Book.series_id == series.id).count()
    return _series_to_dict(series, book_count=count)
