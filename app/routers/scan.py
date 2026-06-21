import json
import re
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_contributor
from app.book_utils import book_to_dict
from app.covers import fetch_and_save
from app.database import get_db, SessionLocal
from app.lookup import lookup_isbn, classify_isbn, _SOURCE_FNS, _lookup_isbndb, _lookup_openlibrary_search
from app.models import Book
from app.schemas import ScanRequest
from app.series_logic import get_or_create_series

router = APIRouter()


def _normalize_isbn(isbn: str) -> str:
    return re.sub(r"[\s\-]", "", isbn)




async def _enrich_book(book_id: int, isbn: str) -> None:
    """Lookup + mise à jour du livre en arrière-plan."""
    db = SessionLocal()
    try:
        info = await lookup_isbn(isbn, db=db)
        book = db.query(Book).filter(Book.id == book_id).first()
        if not book:
            return

        if info is None:
            book.enrichment_status = "not_found"
            db.commit()
            return

        book.title = info["title"]
        book.subtitle = info.get("subtitle")
        book.authors = json.dumps(info.get("authors") or [])
        book.publisher = info.get("publisher")
        book.publish_date = info.get("publish_date")
        book.cover_url = info.get("cover_url")
        book.description = info.get("description")
        book.page_count = info.get("page_count")
        book.language = info.get("language")
        book.source = info.get("source", "openlibrary")
        book.work_key = info.get("work_key")
        # Couverture — on est déjà dans un contexte async, await direct
        if info.get("cover_url"):
            local = await fetch_and_save(isbn, info["cover_url"])
            if local:
                book.cover_url = local

        book.enrichment_status = "ok"
        if info.get("_per_source"):
            book.source_data = json.dumps(info["_per_source"], ensure_ascii=False)

        if info.get("series_name"):
            series = get_or_create_series(db, info["series_name"], source=info.get("source", "openlibrary"))
            book.series_id = series.id
        if info.get("series_position"):
            book.series_position = info["series_position"]

        db.commit()

    finally:
        db.close()




@router.post("/api/scan")
async def scan_isbn(
    body: ScanRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    isbn = _normalize_isbn(body.isbn)

    existing = db.query(Book).filter(Book.isbn == isbn).first()
    if existing:
        return {"status": "exists", "book": book_to_dict(existing)}

    # Sauvegarde immédiate — le titre est l'ISBN en attendant l'enrichissement
    book = Book(
        isbn=isbn,
        title=isbn,
        authors=json.dumps([]),
        source="pending",
        shelf=body.shelf,
        location_id=body.location_id,
        room_id=body.room_id,
        added_at=datetime.utcnow(),
        enrichment_status="pending",
    )
    db.add(book)
    db.commit()
    db.refresh(book)

    background_tasks.add_task(_enrich_book, book.id, isbn)

    info = classify_isbn(isbn)
    return {"status": "pending", "book": book_to_dict(book), "ean_warning": info.get("warning")}


@router.post("/api/books/{book_id}/re-enrich")
async def re_enrich_book(
    book_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Relance la recherche d'infos pour un livre (not_found ou données incomplètes)."""
    user = get_current_user(request, db)
    require_contributor(user)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Livre introuvable")
    if not book.isbn:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Ce livre n'a pas d'ISBN")
    book.enrichment_status = "pending"
    db.commit()
    background_tasks.add_task(_enrich_book, book.id, book.isbn)
    return {"status": "pending", "book_id": book.id}


@router.post("/api/books/{book_id}/re-enrich-source/{source_id}")
async def re_enrich_book_source(
    book_id: int,
    source_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Recharge les données d'un livre depuis une source spécifique et met à jour source_data."""
    from fastapi import HTTPException
    import httpx, asyncio, app.settings as cfg
    user = get_current_user(request, db)
    require_contributor(user)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")
    if not book.isbn:
        raise HTTPException(status_code=400, detail="Ce livre n'a pas d'ISBN")

    isbndb_key = cfg.get(db, "isbndb_api_key") or ""
    googlebooks_key = cfg.get(db, "googlebooks_api_key") or ""
    from app.lookup import _lookup_google
    from app.lookup import classify_isbn as _cls

    variants = _cls(book.isbn)["variants"]
    result = None

    async with httpx.AsyncClient(timeout=15) as client:
        for variant in variants:
            if source_id == "isbndb":
                if not isbndb_key:
                    raise HTTPException(status_code=400, detail="Clé ISBNdb non configurée")
                result = await _lookup_isbndb(client, variant, isbndb_key)
            elif source_id == "googlebooks":
                result = await _lookup_google(client, variant, googlebooks_key)
            elif source_id == "openlibrary_search":
                result = await _lookup_openlibrary_search(client, variant)
            elif source_id in _SOURCE_FNS:
                result = await _SOURCE_FNS[source_id](client, variant)
            else:
                raise HTTPException(status_code=400, detail=f"Source inconnue : {source_id}")
            if result:
                break

    # Mettre à jour source_data
    existing = json.loads(book.source_data) if book.source_data else {}
    if result:
        _KEEP = ("title", "subtitle", "authors", "publisher", "publish_date",
                 "language", "page_count", "series_name", "series_position", "source")
        existing[source_id] = {k: v for k, v in result.items() if k in _KEEP}
    else:
        existing.pop(source_id, None)
    book.source_data = json.dumps(existing, ensure_ascii=False)
    db.commit()

    return {
        "found": result is not None,
        "source_id": source_id,
        "data": existing.get(source_id),
        "source_data": existing,
    }


@router.post("/api/books/re-enrich-all")
async def re_enrich_all(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    force: bool = False,
):
    """Relance la recherche.
    force=False : seulement les livres non trouvés ou sans titre réel.
    force=True  : tous les livres avec un ISBN.
    """
    user = get_current_user(request, db)
    require_contributor(user)

    if force:
        books = db.query(Book).filter(Book.isbn.isnot(None)).all()
    else:
        books = db.query(Book).filter(
            (Book.enrichment_status == "not_found") |
            (Book.enrichment_status == "pending")
        ).all()
        for b in db.query(Book).filter(Book.enrichment_status == "ok").all():
            if b.isbn and b.title == b.isbn:
                books.append(b)

    seen = set()
    unique = []
    for b in books:
        if b.id not in seen and b.isbn:
            seen.add(b.id)
            unique.append(b)
    for b in unique:
        b.enrichment_status = "pending"
    db.commit()
    for b in unique:
        background_tasks.add_task(_enrich_book, b.id, b.isbn)
    return {"queued": len(unique), "book_ids": [b.id for b in unique]}


@router.get("/api/scan/status/{book_id}")
async def scan_status(book_id: int, request: Request, db: Session = Depends(get_db)):
    """Polling : retourne le statut actuel d'un livre en cours d'enrichissement."""
    get_current_user(request, db)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        return {"enrichment_status": "not_found"}
    return {"enrichment_status": book.enrichment_status, "book": book_to_dict(book)}
