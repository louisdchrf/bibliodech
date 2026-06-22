import json
import re
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app.audit import log as audit_log
from app.auth import get_current_user, require_contributor
from app.book_utils import book_to_dict
from app.covers import fetch_and_save
from app.database import get_db, SessionLocal
from app.lookup import lookup_isbn, classify_isbn, _SOURCE_FNS, _lookup_isbndb, _lookup_openlibrary_search
from app.models import Book
from app.schemas import ScanRequest

router = APIRouter()


def _normalize_isbn(isbn: str) -> str:
    return re.sub(r"[\s\-]", "", isbn)




async def _decitre_cover_url(client, isbn: str) -> str | None:
    """Récupère l'URL de couverture depuis la page Decitre (JSON-LD)."""
    import re as _re
    _JSONLD_RE = _re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', _re.DOTALL)
    try:
        resp = await client.get(f"https://www.decitre.fr/livres/{isbn}.html", follow_redirects=True)
        if resp.status_code != 200:
            return None
        for match in _JSONLD_RE.finditer(resp.text):
            try:
                obj = json.loads(match.group(1))
                items = obj if isinstance(obj, list) else [obj]
                for item in items:
                    if item.get("@type") in ("Book", "Product") and item.get("name"):
                        img = item.get("image")
                        if isinstance(img, list):
                            img = img[0] if img else None
                        if img:
                            return img
            except (json.JSONDecodeError, AttributeError):
                continue
    except Exception:
        pass
    return None


async def _resolve_cover(isbn: str, info: dict) -> str | None:
    """Essaie les URLs de couverture dans l'ordre jusqu'à en trouver une valide."""
    import httpx
    import app.settings as cfg_mod
    from app.database import SessionLocal as _SL
    _db = _SL()
    try:
        gb_key = cfg_mod.get(_db, "googlebooks_api_key") or ""
    finally:
        _db.close()

    candidates = []

    # 1. URL fournie par la source principale (scan enrichissement)
    if info.get("cover_url"):
        candidates.append(info["cover_url"])

    async with httpx.AsyncClient(timeout=8) as client:
        # 2. Decitre — bonne couverture pour le fonds francophone
        decitre_url = await _decitre_cover_url(client, isbn)
        if decitre_url and decitre_url not in candidates:
            candidates.append(decitre_url)

        # 3. Open Library covers par ISBN
        ol_url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
        if ol_url not in candidates:
            candidates.append(ol_url)

        # 4. Google Books thumbnail
        gb_api = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
        if gb_key:
            gb_api += f"&key={gb_key}"
        try:
            gb_resp = await client.get(gb_api)
            if gb_resp.status_code == 200:
                items = gb_resp.json().get("items", [])
                if items:
                    links = items[0].get("volumeInfo", {}).get("imageLinks", {})
                    gb_cover = (links.get("large") or links.get("medium") or links.get("thumbnail", "")).replace("http://", "https://")
                    if gb_cover and gb_cover not in candidates:
                        candidates.append(gb_cover)
        except Exception:
            pass

    for url in candidates:
        local = await fetch_and_save(isbn, url)
        if local:
            return local

    return None


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
            from app.applog import log_error
            log_error(db, f"ISBN introuvable dans toutes les sources : {isbn}", category="scan", detail={"isbn": isbn, "book_id": book_id})
            return

        book.title = info["title"]
        book.subtitle = info.get("subtitle")
        book.authors = json.dumps(info.get("authors") or [])
        book.publisher = info.get("publisher")
        book.publish_date = info.get("publish_date")
        book.description = info.get("description")
        book.page_count = info.get("page_count")
        book.language = info.get("language")
        book.source = info.get("source", "openlibrary")
        book.work_key = info.get("work_key")

        # Couverture — chaîne de fallback
        book.cover_url = await _resolve_cover(isbn, info)

        book.enrichment_status = "ok"
        book.enrichment_source = info.get("source")
        if info.get("_per_source"):
            book.source_data = json.dumps(info["_per_source"], ensure_ascii=False)

        audit_log(db, book.id, "enriched", detail={"source": info.get("source"), "title": info.get("title")})
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
    audit_log(db, book.id, "created", user_id=user.id, detail={"isbn": isbn})
    db.commit()

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
    if unique:
        from app import scheduler as sched
        from datetime import datetime, timezone
        sched._running["reenrich-bg"] = {
            "label": f"Enrichissement ({len(unique)} livres)",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        for b in unique:
            background_tasks.add_task(_enrich_book, b.id, b.isbn)
        background_tasks.add_task(lambda: sched._running.pop("reenrich-bg", None))
    return {"queued": len(unique), "book_ids": [b.id for b in unique]}


@router.get("/api/scan/status/{book_id}")
async def scan_status(book_id: int, request: Request, db: Session = Depends(get_db)):
    """Polling : retourne le statut actuel d'un livre en cours d'enrichissement."""
    get_current_user(request, db)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        return {"enrichment_status": "not_found"}
    return {"enrichment_status": book.enrichment_status, "book": book_to_dict(book)}
