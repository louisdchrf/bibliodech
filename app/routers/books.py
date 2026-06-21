import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.audit import log as audit_log
from app.auth import get_current_user, require_admin, require_contributor
from app.book_utils import book_to_dict
from app.database import get_db
from app.models import Book, Series
from app.schemas import BookCreate, BookUpdate, BulkActionRequest

router = APIRouter()


@router.get("/api/books")
def list_books(
    request: Request,
    search: Optional[str] = Query(None),
    series_id: Optional[int] = Query(None),
    exclude_series_id: Optional[int] = Query(None),
    room_id: Optional[str] = Query(None),  # int ou "none" pour livres sans localisation
    source: Optional[str] = Query(None),   # filtre par source d'enrichissement
    sort_by: str = Query("title"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    get_current_user(request, db)
    q = db.query(Book)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (Book.title.ilike(like)) | (Book.authors.ilike(like)) | (Book.isbn.ilike(like))
        )
    if series_id is not None:
        q = q.filter(Book.series_id == series_id)
    if exclude_series_id is not None:
        q = q.filter((Book.series_id != exclude_series_id) | Book.series_id.is_(None))
    if room_id == "none":
        q = q.filter(Book.room_id.is_(None))
    elif room_id is not None:
        q = q.filter(Book.room_id == int(room_id))
    if source == "none":
        q = q.filter(Book.enrichment_source.is_(None))
    elif source is not None:
        q = q.filter(Book.enrichment_source == source)

    if sort_by == "author":
        q = q.order_by(Book.authors)
    elif sort_by == "added_at":
        q = q.order_by(Book.added_at.desc())
    else:
        q = q.order_by(Book.title)

    total = q.count()
    books = q.offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [book_to_dict(b) for b in books],
    }


SOURCE_LABELS = {
    "sudoc": "SUDOC", "bnf": "BnF", "decitre": "Decitre",
    "isbndb": "ISBNdb", "openlibrary": "Open Library",
    "openlibrary_search": "Open Library (search)", "googlebooks": "Google Books",
}

@router.get("/api/books/sources")
def list_sources(request: Request, db: Session = Depends(get_db)):
    """Retourne les sources d'enrichissement utilisées avec leur nombre de livres."""
    from sqlalchemy import func
    get_current_user(request, db)
    rows = db.query(Book.enrichment_source, func.count(Book.id))\
        .group_by(Book.enrichment_source)\
        .order_by(func.count(Book.id).desc()).all()
    return [
        {"id": src or "none", "label": SOURCE_LABELS.get(src, src or "Manuel / Import"), "count": cnt}
        for src, cnt in rows
    ]


@router.get("/api/books/{book_id}")
def get_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")
    return book_to_dict(book)


@router.post("/api/books", status_code=status.HTTP_201_CREATED)
def create_book(body: BookCreate, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)

    book = Book(
        isbn=body.isbn,
        title=body.title,
        subtitle=body.subtitle,
        authors=json.dumps(body.authors or []),
        publisher=body.publisher,
        publish_date=body.publish_date,
        cover_url=body.cover_url,
        description=body.description,
        page_count=body.page_count,
        language=body.language,
        source=body.source,
        work_key=body.work_key,
        series_id=body.series_id,
        series_position=body.series_position,
        shelf=body.shelf,
        added_at=datetime.utcnow(),
    )
    db.add(book)
    db.commit()
    db.refresh(book)
    audit_log(db, book.id, "created", user_id=user.id, detail={"title": book.title, "isbn": book.isbn})
    db.commit()
    return book_to_dict(book)


@router.put("/api/books/{book_id}")
def update_book(
    book_id: int,
    body: BookUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")

    update_data = body.model_dump(exclude_unset=True)
    changed = {}
    for field, value in update_data.items():
        old = getattr(book, field, None)
        if field == "authors" and isinstance(value, list):
            new_val = json.dumps(value)
            if new_val != old:
                changed[field] = {"from": old, "to": new_val}
            setattr(book, field, new_val)
        else:
            if value != old:
                changed[field] = {"from": str(old) if old is not None else None, "to": str(value) if value is not None else None}
            setattr(book, field, value)

    if changed:
        audit_log(db, book.id, "updated", user_id=user.id, detail=changed)
    db.commit()
    db.refresh(book)
    return book_to_dict(book)


@router.patch("/api/books/{book_id}")
def patch_book(
    book_id: int,
    body: BookUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "authors" and isinstance(value, list):
            setattr(book, field, json.dumps(value))
        else:
            setattr(book, field, value)
    db.commit()
    db.refresh(book)
    return book_to_dict(book)


@router.post("/api/books/clean-authors")
def clean_authors(request: Request, db: Session = Depends(get_db)):
    """Normalise les auteurs en format 'Nom, Prénom' → 'Prénom Nom' pour tous les livres."""
    user = get_current_user(request, db)
    require_contributor(user)

    def _normalize(author: str) -> str:
        # Institution ou format complexe : laisser tel quel
        if '(' in author or author.count(',') != 1:
            return author
        nom, prenom = author.split(',', 1)
        prenom = prenom.strip()
        nom = nom.strip()
        if not prenom:
            return author
        return f"{prenom} {nom}"

    updated = 0
    books = db.query(Book).filter(Book.authors.isnot(None)).all()
    for book in books:
        try:
            authors = json.loads(book.authors)
        except Exception:
            continue
        cleaned = [_normalize(a) for a in authors]
        if cleaned != authors:
            book.authors = json.dumps(cleaned, ensure_ascii=False)
            updated += 1

    db.commit()
    return {"updated": updated}


@router.post("/api/books/bulk")
def bulk_books(
    body: BulkActionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    books = db.query(Book).filter(Book.id.in_(body.ids)).all()
    if not books:
        raise HTTPException(status_code=404, detail="Aucun livre trouvé")

    if body.action == "delete":
        require_admin(user)
        for b in books:
            db.delete(b)
        db.commit()
        return {"deleted": len(books)}

    if body.action == "update":
        d = body.data
        if d is None:
            raise HTTPException(status_code=400, detail="data requis pour action update")
        for b in books:
            if d.authors is not None:
                b.authors = json.dumps(d.authors)
            if d.series_id is not None:
                b.series_id = d.series_id
            if d.series_position is not None:
                b.series_position = d.series_position
            if d.room_id is not None:
                b.room_id = d.room_id
        db.commit()
        return {"updated": len(books)}

    raise HTTPException(status_code=400, detail="Action inconnue")


@router.get("/api/logs")
def get_all_logs(request: Request, db: Session = Depends(get_db), limit: int = 500):
    get_current_user(request, db)
    from app.models import AuditLog
    logs = (
        db.query(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": l.id,
            "book_id": l.book_id,
            "book_title": l.book.title if l.book else None,
            "action": l.action,
            "username": l.user.username if l.user else None,
            "detail": json.loads(l.detail) if l.detail else None,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]


@router.get("/api/books/{book_id}/logs")
def get_book_logs(book_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    from app.models import AuditLog, User
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.book_id == book_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": l.id,
            "action": l.action,
            "username": l.user.username if l.user else None,
            "detail": json.loads(l.detail) if l.detail else None,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]


@router.delete("/api/books/{book_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)

    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")
    db.delete(book)
    db.commit()


@router.get("/api/shelves")
def list_shelves(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    rows = (
        db.query(Book.shelf)
        .filter(Book.shelf != None, Book.shelf != "")  # noqa: E711
        .distinct()
        .order_by(Book.shelf)
        .all()
    )
    return [r[0] for r in rows]


@router.get("/api/tasks/schedules")
def get_task_schedules(request: Request, db: Session = Depends(get_db)):
    from app.auth import require_admin
    user = get_current_user(request, db)
    require_admin(user)
    from app import scheduler as sched
    return sched.get_schedules(db)


@router.post("/api/tasks/schedules")
def update_task_schedules(body: dict, request: Request, db: Session = Depends(get_db)):
    from app.auth import require_admin
    user = get_current_user(request, db)
    require_admin(user)
    from app import scheduler as sched
    schedules = sched.get_schedules(db)
    for task_id, cfg in body.items():
        if task_id in schedules:
            if "enabled" in cfg:
                schedules[task_id]["enabled"] = bool(cfg["enabled"])
            if "interval_minutes" in cfg:
                schedules[task_id]["interval_minutes"] = max(1, int(cfg["interval_minutes"]))
    sched.save_schedules(db, schedules)
    sched.reload(db)
    return schedules
