import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
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
    site_id: Optional[str] = Query(None),    # filtre sur une adresse entière
    room_id: Optional[str] = Query(None),    # int ou "none" pour livres sans localisation
    source: Optional[str] = Query(None),     # filtre par source d'enrichissement
    series_id: Optional[str] = Query(None),  # int, "none" (sans série), ou "any" (avec série)
    genre: Optional[str] = Query(None),      # valeur exacte ou "none"
    has_cover: Optional[str] = Query(None),  # "yes" ou "no"
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
    if site_id is not None and site_id.isdigit():
        from app.models import Room
        room_ids_in_site = [r.id for r in db.query(Room).filter(Room.site_id == int(site_id)).all()]
        q = q.filter(Book.room_id.in_(room_ids_in_site))
    if room_id == "none":
        q = q.filter(Book.room_id.is_(None))
    elif room_id is not None:
        parts = [p.strip() for p in room_id.split(",") if p.strip().lstrip("-").isdigit()]
        if len(parts) == 1:
            q = q.filter(Book.room_id == int(parts[0]))
        elif parts:
            q = q.filter(Book.room_id.in_([int(p) for p in parts]))
    if source == "none":
        q = q.filter(Book.enrichment_source.is_(None))
    elif source is not None:
        q = q.filter(Book.enrichment_source == source)

    if series_id == "none":
        q = q.filter(Book.series_id.is_(None))
    elif series_id == "any":
        q = q.filter(Book.series_id.isnot(None))
    elif series_id is not None:
        q = q.filter(Book.series_id == int(series_id))

    if genre == "none":
        q = q.filter(Book.genre.is_(None))
    elif genre is not None:
        q = q.filter(Book.genre == genre)

    if has_cover == "no":
        q = q.filter(Book.cover_url.is_(None))
    elif has_cover == "yes":
        q = q.filter(Book.cover_url.isnot(None))

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


@router.get("/api/books/genres")
def list_genres(request: Request, db: Session = Depends(get_db)):
    """Retourne les genres présents en base avec leur nombre de livres."""
    from sqlalchemy import func
    get_current_user(request, db)
    rows = db.query(Book.genre, func.count(Book.id))\
        .filter(Book.genre.isnot(None))\
        .group_by(Book.genre)\
        .order_by(func.count(Book.id).desc()).all()
    return [{"id": g, "count": cnt} for g, cnt in rows]


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
        shelf=body.shelf,
        added_at=datetime.now(timezone.utc),
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


def _clean_authors_logic(db, task_id: str = "clean-authors") -> str:
    from app import scheduler as sched

    def _normalize(author: str) -> str:
        if '(' in author or author.count(',') != 1:
            return author
        nom, prenom = author.split(',', 1)
        prenom, nom = prenom.strip(), nom.strip()
        if not prenom:
            return author
        return f"{prenom} {nom}"

    books = db.query(Book).filter(Book.authors.isnot(None)).all()
    total = len(books)
    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    updated = 0
    for i, book in enumerate(books):
        try:
            authors = json.loads(book.authors)
        except Exception:
            continue
        cleaned = [_normalize(a) for a in authors]
        if cleaned != authors:
            book.authors = json.dumps(cleaned, ensure_ascii=False)
            updated += 1
        if task_id in sched._running:
            sched._running[task_id]["progress"]["current"] = i + 1
    db.commit()
    return f"{updated} livre(s) mis à jour"


@router.post("/api/books/clean-authors")
def clean_authors(request: Request, db: Session = Depends(get_db)):
    """Normalise les auteurs en format 'Nom, Prénom' → 'Prénom Nom' pour tous les livres."""
    user = get_current_user(request, db)
    require_contributor(user)
    _clean_authors_logic(db)
    return {"ok": True}


async def _enrich_genres_logic(db, task_id: str = "enrich-genres") -> dict:
    """Interroge SUDOC puis BnF pour récupérer le genre des livres qui n'en ont pas encore."""
    from app.lookup import _lookup_genre_unimarc, _lookup_sudoc
    import app.settings as cfg
    from app import scheduler as sched
    import httpx

    # Sources actives selon la configuration
    sources_cfg = cfg.get(db, "lookup_sources") or []
    active_source_ids = {s["id"] for s in sources_cfg if s.get("enabled", True)}
    use_sudoc = "sudoc" in active_source_ids
    use_bnf   = "bnf" in active_source_ids

    books = db.query(Book).filter(Book.isbn.isnot(None)).all()
    total = len(books)
    updated = 0

    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    async with httpx.AsyncClient(timeout=10) as client:
        for i, book in enumerate(books):
            genre = None
            try:
                if use_sudoc:
                    info = await _lookup_sudoc(client, book.isbn)
                    if info:
                        genre = info.get("genre")
                if not genre and use_bnf:
                    genre = await _lookup_genre_unimarc(client, book.isbn)
            except Exception:
                pass
            if genre and genre.lower() not in ("texte imprimé", "text", "texte"):
                book.genre = genre
                updated += 1
                if updated % 20 == 0:
                    db.commit()
            if task_id in sched._running:
                sched._running[task_id]["progress"]["current"] = i + 1

    db.commit()
    return {"total": total, "updated": updated}


async def _refresh_covers_logic(db, task_id: str = "refresh-covers") -> dict:
    """Re-télécharge toutes les couvertures existantes en haute résolution."""
    import asyncio
    from app.routers.scan import _resolve_cover
    from app.covers import fetch_and_save
    from app import scheduler as sched

    books = db.query(Book).filter(Book.isbn.isnot(None)).all()
    total = len(books)
    updated = 0
    done = 0
    sem = asyncio.Semaphore(3)

    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    async def _try_one(book):
        nonlocal updated, done
        async with sem:
            url = await _resolve_cover(book.isbn, {})
            if url:
                result = await fetch_and_save(book.isbn, url)
                if result:
                    book.cover_url = result
                    db.commit()
                    updated += 1
            done += 1
            if task_id in sched._running:
                sched._running[task_id]["progress"]["current"] = done

    await asyncio.gather(*[_try_one(b) for b in books])
    return {"updated": updated, "total": total}


async def _fetch_covers_logic(db, task_id: str = "fetch-covers") -> dict:
    """Cherche et sauvegarde les couvertures pour les livres qui n'en ont pas."""
    import asyncio
    from app.routers.scan import _resolve_cover
    from app import scheduler as sched

    books = db.query(Book).filter(
        (Book.cover_url.is_(None)) | (Book.cover_url == "")
    ).filter(Book.isbn.isnot(None)).all()

    total = len(books)
    updated = 0
    done = 0
    sem = asyncio.Semaphore(3)

    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    async def _try_one(book):
        nonlocal updated, done
        async with sem:
            url = await _resolve_cover(book.isbn, {})
            if url:
                book.cover_url = url
                db.commit()
                updated += 1
            done += 1
            if task_id in sched._running:
                sched._running[task_id]["progress"] = {"current": done, "total": total}

    await asyncio.gather(*[_try_one(b) for b in books])
    return {"updated": updated, "total": total}


@router.post("/api/books/bulk")
def bulk_books(
    body: BulkActionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    # SQLite limite le nombre de variables dans IN() à ~999 — on découpe en chunks
    CHUNK = 900
    ids = list(body.ids)
    books = []
    for i in range(0, len(ids), CHUNK):
        books += db.query(Book).filter(Book.id.in_(ids[i:i+CHUNK])).all()
    if not books:
        raise HTTPException(status_code=404, detail="Aucun livre trouvé")

    if body.action == "delete":
        require_admin(user)
        for i in range(0, len(ids), CHUNK):
            db.query(Book).filter(Book.id.in_(ids[i:i+CHUNK])).delete(synchronize_session=False)
        db.commit()
        return {"deleted": len(ids)}

    if body.action == "update":
        d = body.data
        if d is None:
            raise HTTPException(status_code=400, detail="data requis pour action update")
        for b in books:
            if d.authors is not None:
                b.authors = json.dumps(d.authors)
            if d.room_id is not None:
                b.room_id = d.room_id
            if d.series_id is not None:
                b.series_id = d.series_id
            if d.series_position is not None:
                b.series_position = d.series_position
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
            "created_at": l.created_at.isoformat() + "Z" if l.created_at else None,
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
            "created_at": l.created_at.isoformat() + "Z" if l.created_at else None,
        }
        for l in logs
    ]


@router.get("/api/applogs")
def get_app_logs(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 200,
    category: str | None = None,
    level: str | None = None,
):
    from app.auth import require_admin
    user = get_current_user(request, db)
    require_admin(user)
    limit = min(limit, 1000)
    from app.models import AppLog
    q = db.query(AppLog).order_by(AppLog.created_at.desc())
    if category:
        q = q.filter(AppLog.category == category)
    if level:
        q = q.filter(AppLog.level == level)
    logs = q.limit(limit).all()
    return [
        {
            "id": l.id,
            "level": l.level,
            "category": l.category,
            "message": l.message,
            "detail": json.loads(l.detail) if l.detail else None,
            "created_at": l.created_at.isoformat() + "Z" if l.created_at else None,
        }
        for l in logs
    ]


@router.post("/api/books/{book_id}/cover")
async def upload_cover(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
):
    """Remplace la couverture d'un livre par un fichier ou une URL."""
    user = get_current_user(request, db)
    require_contributor(user)

    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")

    from app.covers import COVERS_DIR, MAX_WIDTH, QUALITY
    from PIL import Image
    import io as _io

    isbn = book.isbn or str(book.id)
    path = f"{COVERS_DIR}/{isbn}.jpg"

    if file and file.filename:
        content = await file.read()
        try:
            img = Image.open(_io.BytesIO(content)).convert("RGB")
        except Exception:
            raise HTTPException(status_code=400, detail="Fichier image invalide")
        if img.width > MAX_WIDTH:
            ratio = MAX_WIDTH / img.width
            img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
        img.save(path, "JPEG", quality=QUALITY, optimize=True)
        book.cover_url = f"/covers/{isbn}.jpg"

    elif url:
        from app.covers import fetch_and_save
        result = await fetch_and_save(isbn, url)
        if not result:
            raise HTTPException(status_code=400, detail="Impossible de télécharger l'image depuis cette URL")
        book.cover_url = result

    else:
        raise HTTPException(status_code=400, detail="Fichier ou URL requis")

    audit_log(db, book.id, "cover_updated", user_id=user.id)
    db.commit()
    db.refresh(book)
    return book_to_dict(book)


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


@router.get("/api/activity")
def get_activity(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    from app import scheduler as sched
    return sched.get_running()


@router.post("/api/activity/cancel")
def cancel_activity(request: Request, db: Session = Depends(get_db)):
    from app.auth import require_contributor
    user = get_current_user(request, db)
    require_contributor(user)
    from app import scheduler as sched
    sched._running.clear()
    return {"ok": True}


@router.get("/api/tasks/next-runs")
def get_next_runs(request: Request, db: Session = Depends(get_db)):
    from app.auth import require_admin
    user = get_current_user(request, db)
    require_admin(user)
    from app import scheduler as sched
    return sched.get_next_runs()


@router.post("/api/tasks/run/{task_id}")
async def run_task_manual(task_id: str, request: Request, db: Session = Depends(get_db)):
    """Lance une tâche manuellement (visible dans l'indicateur d'activité)."""
    from app.auth import require_contributor
    user = get_current_user(request, db)
    require_contributor(user)
    from app import scheduler as sched
    from app.applog import log_task, log_error
    sched._running[task_id] = {
        "label": sched.SCHEDULABLE_TASKS.get(task_id, task_id),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    started = datetime.now(timezone.utc)
    log_task(db, task_id, "started", {"triggered_by": user.username})
    try:
        result = await sched._execute_task(task_id, db)
    except Exception as e:
        sched._running.pop(task_id, None)
        log_error(db, f"Tâche '{task_id}' échouée : {e}", category="task", detail={"task_id": task_id})
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))
    sched._running.pop(task_id, None)
    elapsed = round((datetime.now(timezone.utc) - started).total_seconds())
    log_task(db, task_id, "done", {"result": result, "elapsed_s": elapsed, "triggered_by": user.username})
    return {"result": result}


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
