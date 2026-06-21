import json
from sqlalchemy.orm import Session
from app.models import Series, Book


def get_or_create_series(db: Session, name: str, source: str = "openlibrary") -> Series:
    series = db.query(Series).filter(Series.name == name).first()
    if not series:
        series = Series(name=name, source=source)
        db.add(series)
        db.commit()
        db.refresh(series)
    return series


def suggest_series_for_book(db: Session, book_id: int) -> list[dict]:
    """Return other books by the same authors that have no series assigned."""
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book or not book.authors:
        return []

    try:
        authors = json.loads(book.authors)
    except Exception:
        authors = [book.authors]

    if not authors:
        return []

    # Find books with no series that share at least one author
    candidates = (
        db.query(Book)
        .filter(Book.series_id == None, Book.id != book_id)  # noqa: E711
        .all()
    )

    results = []
    for candidate in candidates:
        if not candidate.authors:
            continue
        try:
            cand_authors = json.loads(candidate.authors)
        except Exception:
            cand_authors = [candidate.authors]
        if any(a in cand_authors for a in authors):
            results.append({
                "book_id": candidate.id,
                "title": candidate.title,
                "authors": cand_authors,
            })

    return results
