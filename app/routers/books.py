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
    room_id: Optional[str] = Query(None),  # int ou "none" pour livres sans localisation
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
    if room_id == "none":
        q = q.filter(Book.room_id.is_(None))
    elif room_id is not None:
        q = q.filter(Book.room_id == int(room_id))

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
