import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app.audit import log as audit_log
from app.auth import get_current_user, require_contributor
from app.book_utils import book_to_dict
from app.covers import fetch_and_save
from app.database import get_db, SessionLocal
from app.lookup import lookup_isbn, classify_isbn, _SOURCE_FNS, _lookup_isbndb, _lookup_openlibrary_search, _normalize_isbn, _lookup_sudoc
from app.models import Book
from app.schemas import ScanRequest

router = APIRouter()



async def _decitre_cover_url(client, isbn: str) -> str | None:
    """Récupère l'URL de couverture depuis la page Decitre (pattern di-static CDN)."""
    from app.lookup import _DECITRE_HEADERS
    # L'URL de couverture principale (sans suffixe _N) est encodée dans le HTML
    # sous la forme : products-images.di-static.com/image/{slug}/{isbn}-{size}-1.webp
    # On prend la plus grande résolution disponible (475x500 > 200x303 > 120x160)
    _SIZES = ["475x500", "200x303", "120x160"]
    try:
        resp = await client.get(
            f"https://www.decitre.fr/livres/{isbn}.html",
            headers=_DECITRE_HEADERS,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        # Chercher le pattern avec l'ISBN exact (couverture principale, pas _N)
        for size in _SIZES:
            m = re.search(
                r'(https://products-images\.di-static\.com/image/[^"\'>\s]+/'
                + re.escape(isbn) + r'-' + re.escape(size) + r'-1\.(webp|jpg|png))',
                resp.text,
            )
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


async def _resolve_cover(isbn: str, info: dict, db=None) -> str | None:
    """Essaie les URLs de couverture dans l'ordre jusqu'à en trouver une valide."""
    import httpx
    import app.settings as cfg_mod
    if db is None:
        from app.database import SessionLocal as _SL
        _db = _SL()
        try:
            gb_key = cfg_mod.get(_db, "googlebooks_api_key") or ""
        finally:
            _db.close()
    else:
        gb_key = cfg_mod.get(db, "googlebooks_api_key") or ""

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


async def _enrich_book(book_id: int, isbn: str, _progress_key: str | None = None, sources: list | None = None) -> None:
    """Lookup + mise à jour du livre en arrière-plan."""
    db = SessionLocal()
    try:
        info = await lookup_isbn(isbn, db=db, sources=sources)
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
        if info.get("genre") and not book.genre:
            book.genre = info["genre"]

        # Couverture — chaîne de fallback
        book.cover_url = await _resolve_cover(isbn, info, db=db)

        book.enrichment_status = "ok"
        book.enrichment_source = info.get("source")
        if info.get("_per_source"):
            book.source_data = json.dumps(info["_per_source"], ensure_ascii=False)

        audit_log(db, book.id, "enriched", detail={"source": info.get("source"), "title": info.get("title")})
        db.commit()

        # Chercher la série dans SUDOC si le livre n'en a pas encore
        if book.series_id is None:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=6.0) as _c:
                _sudoc_info = await _lookup_sudoc(_c, isbn)
            if _sudoc_info and _sudoc_info.get("series_name"):
                from app.models import Series
                from app.routers.series import _norm
                s_name = _sudoc_info["series_name"]
                s_vol = _sudoc_info.get("series_position")
                existing = db.query(Series).all()
                series = next((s for s in existing if _norm(s.name) == _norm(s_name)), None)
                if not series:
                    series = Series(name=s_name, source="sudoc")
                    db.add(series)
                    db.flush()
                book.series_id = series.id
                if s_vol is not None and book.series_position is None:
                    book.series_position = float(s_vol)
                db.commit()

    finally:
        if _progress_key:
            from app import scheduler as sched
            if _progress_key in sched._running:
                p = sched._running[_progress_key].get("progress")
                if p:
                    p["current"] = min(p["current"] + 1, p["total"])
                    if p["current"] >= p["total"]:
                        sched._running.pop(_progress_key, None)
        db.close()


async def _reenrich_missing(db, force: bool = False, task_id: str = "reenrich") -> dict:
    """Enrichit les livres manquants en séquence (usage scheduler, avec progression)."""
    from app import scheduler as sched

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

    seen, unique = set(), []
    for b in books:
        if b.id not in seen and b.isbn:
            seen.add(b.id)
            unique.append(b)
    for b in unique:
        b.enrichment_status = "pending"
    db.commit()

    total = len(unique)
    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    for b in unique:
        await _enrich_book(b.id, b.isbn, _progress_key=task_id)

    return {"queued": total}




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
        added_at=datetime.now(timezone.utc),
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
    sources: str = "",
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
    src_filter = [s.strip() for s in sources.split(",") if s.strip()] if sources else None
    background_tasks.add_task(_enrich_book, book.id, book.isbn, None, src_filter)
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


@router.post("/api/books/{book_id}/apply-source/{source_id}")
async def apply_book_source(
    book_id: int,
    source_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Applique les données d'une source stockée dans source_data sur le livre."""
    from fastapi import HTTPException
    user = get_current_user(request, db)
    require_contributor(user)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")
    sd = json.loads(book.source_data) if book.source_data else {}
    data = sd.get(source_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour la source '{source_id}'")

    if data.get("title"):
        book.title = data["title"]
    if data.get("subtitle") is not None:
        book.subtitle = data["subtitle"]
    if data.get("authors"):
        book.authors = json.dumps(data["authors"])
    if data.get("publisher"):
        book.publisher = data["publisher"]
    if data.get("publish_date"):
        book.publish_date = data["publish_date"]
    if data.get("language"):
        book.language = data["language"]
    if data.get("page_count"):
        book.page_count = data["page_count"]
    if data.get("series_name"):
        from app.models import Series
        from app.routers.series import _norm
        s_name = data["series_name"]
        all_series = db.query(Series).all()
        match = next((s for s in all_series if _norm(s.name) == _norm(s_name)), None)
        if not match:
            match = Series(name=s_name, source=source_id)
            db.add(match)
            db.flush()
        book.series_id = match.id
        if data.get("series_position") is not None and book.series_position is None:
            book.series_position = float(data["series_position"])
    book.enrichment_source = source_id
    book.source = source_id

    audit_log(db, book.id, "source_applied", detail={"source": source_id})
    db.commit()

    from app.book_utils import book_to_dict
    return book_to_dict(book)


@router.get("/api/books/{book_id}/lookup-series")
async def lookup_book_series(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Interroge toutes les sources pour trouver la série d'un livre (sans l'appliquer)."""
    from fastapi import HTTPException
    get_current_user(request, db)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")
    if not book.isbn:
        raise HTTPException(status_code=400, detail="Ce livre n'a pas d'ISBN")
    from app.lookup import lookup_isbn
    info = await lookup_isbn(book.isbn, db=db)
    if info and info.get("series_name"):
        return {
            "found": True,
            "series_name": info["series_name"],
            "series_position": info.get("series_position"),
            "source": info.get("source"),
        }
    return {"found": False}


@router.post("/api/books/{book_id}/apply-series")
async def apply_book_series(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Assigne la série et le tome depuis le body {series_name, series_position}."""
    from fastapi import HTTPException
    user = get_current_user(request, db)
    require_contributor(user)
    body = await request.json()
    series_name = (body.get("series_name") or "").strip()
    series_position = body.get("series_position")
    if not series_name:
        raise HTTPException(status_code=400, detail="series_name requis")
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")

    from app.models import Series
    from app.routers.series import _norm
    series = db.query(Series).all()
    match = next((s for s in series if _norm(s.name) == _norm(series_name)), None)
    if not match:
        match = Series(name=series_name, source="lookup")
        db.add(match)
        db.flush()

    book.series_id = match.id
    if series_position is not None:
        book.series_position = float(series_position)

    audit_log(db, book.id, "series_applied", detail={"series": series_name, "position": series_position})
    db.commit()

    from app.book_utils import book_to_dict
    return book_to_dict(book)


@router.post("/api/books/re-enrich-all")
async def re_enrich_all(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    force: bool = False,
    sources: str = "",
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
        import time
        # Bloquer le double-lancement
        if any(k.startswith("reenrich-bg") for k in sched._running):
            return {"queued": 0, "already_running": True}
        key = f"reenrich-bg-{int(time.time())}"
        sched._running[key] = {
            "label": f"Enrichissement ({len(unique)} livres)",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "progress": {"current": 0, "total": len(unique)},
        }
        src_filter = [s.strip() for s in sources.split(",") if s.strip()] if sources else None
        for b in unique:
            background_tasks.add_task(_enrich_book, b.id, b.isbn, key, src_filter)
    return {"queued": len(unique), "book_ids": [b.id for b in unique]}


@router.get("/api/scan/status/{book_id}")
async def scan_status(book_id: int, request: Request, db: Session = Depends(get_db)):
    """Polling : retourne le statut actuel d'un livre en cours d'enrichissement."""
    get_current_user(request, db)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        return {"enrichment_status": "not_found"}
    return {"enrichment_status": book.enrichment_status, "book": book_to_dict(book)}
