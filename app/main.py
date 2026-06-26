import csv
import io
import json
import logging
import os
from fastapi import BackgroundTasks, FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.auth import (
    get_current_user, verify_password, create_session, clear_session, bootstrap_admin, require_admin
)
from app.models import User, Book, Series
from app.routers import scan, books, users, settings as settings_router, locations as locations_router, loans as loans_router, series as series_router, backup as backup_router, music as music_router
from app.lookup import debug_isbn

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

app = FastAPI(title="Bibliodech")

BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

# ── Static files & templates ─────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

COVERS_DIR = os.environ.get("COVERS_DIR", "/app/data/covers")
os.makedirs(COVERS_DIR, exist_ok=True)
app.mount("/covers", StaticFiles(directory=COVERS_DIR), name="covers")

AVATARS_DIR = os.environ.get("AVATARS_DIR", "/app/data/avatars")
os.makedirs(AVATARS_DIR, exist_ok=True)
app.mount("/avatars", StaticFiles(directory=AVATARS_DIR), name="avatars")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(scan.router)
app.include_router(books.router)
app.include_router(users.router)
app.include_router(settings_router.router)
app.include_router(locations_router.router)
app.include_router(loans_router.router)
app.include_router(series_router.router)
app.include_router(backup_router.router)
app.include_router(music_router.router)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    # Ensure data directory exists for SQLite
    db_url = os.environ.get("DATABASE_URL", "sqlite:////app/data/bibliodech.db")
    if db_url.startswith("sqlite:///"):
        path = db_url.replace("sqlite:///", "")
        os.makedirs(os.path.dirname(path), exist_ok=True)
    init_db()
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        bootstrap_admin(db)
        from app import scheduler as sched
        sched.start(db)
    finally:
        db.close()


@app.on_event("shutdown")
def on_shutdown():
    from app import scheduler as sched
    sched.stop()


# ── Page routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=RedirectResponse)
def root():
    return RedirectResponse(url="/library", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "build_version": BUILD_VERSION})


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    from app.auth import check_brute_force, record_failed_login, _client_ip
    from app.models import AppLog
    try:
        check_brute_force(request)
    except HTTPException as e:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": e.detail},
            status_code=429,
        )

    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")[:200]

    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user or not verify_password(password, user.password_hash):
        record_failed_login(request)
        db.add(AppLog(level="warning", category="auth",
                      message=f"Échec de connexion pour « {username} »",
                      detail=json.dumps({"ip": ip, "ua": ua})))
        db.commit()
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Identifiants incorrects"},
            status_code=401,
        )

    db.add(AppLog(level="info", category="auth",
                  message=f"Connexion de « {user.username} »",
                  detail=json.dumps({"ip": ip, "ua": ua})))
    db.commit()

    dest = "/change-password" if user.must_change_password else "/scanner"
    response = RedirectResponse(url=dest, status_code=302)
    create_session(response, user.id)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    clear_session(response)
    return response


def _require_pw_changed(user: User):
    """Retourne une redirection si l'utilisateur doit changer son mot de passe."""
    if user.must_change_password:
        return RedirectResponse(url="/change-password", status_code=302)
    return None


@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "build_version": BUILD_VERSION})


@app.get("/scanner", response_class=HTMLResponse)
def scanner_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    if redir := _require_pw_changed(user): return redir
    return templates.TemplateResponse("scanner.html", {"request": request, "user": user, "active": "scanner", "build_version": BUILD_VERSION})


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    if redir := _require_pw_changed(user): return redir
    return templates.TemplateResponse("library.html", {"request": request, "user": user, "active": "library", "build_version": BUILD_VERSION})



@app.get("/music", response_class=HTMLResponse)
def music_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    if redir := _require_pw_changed(user): return redir
    from app.models import Room
    rooms = [{"id": r.id, "name": r.name} for r in db.query(Room).order_by(Room.name).all()]
    return templates.TemplateResponse("music.html", {
        "request": request, "user": user, "active": "music",
        "rooms": rooms, "build_version": BUILD_VERSION,
    })


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    if redir := _require_pw_changed(user): return redir
    from app.auth import require_admin
    require_admin(user)
    return templates.TemplateResponse("tasks.html", {"request": request, "user": user, "active": "tasks", "build_version": BUILD_VERSION})


@app.get("/api/books/export/csv")
def export_books_csv(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    books = db.query(Book).order_by(Book.title).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ISBN", "Titre", "Sous-titre", "Auteurs", "Éditeur", "Date", "Langue",
                     "Pages", "Localisation", "Ajouté le"])
    for b in books:
        authors = ", ".join(json.loads(b.authors)) if b.authors else ""
        if b.room:
            parts = []
            if b.room.site:
                parts.append(b.room.site.name)
            parts.append(b.room.name)
            loc = " / ".join(parts)
        elif b.location:
            loc = b.location.label
        else:
            loc = b.shelf or ""
        writer.writerow([
            b.isbn or "", b.title, b.subtitle or "", authors, b.publisher or "",
            b.publish_date or "", b.language or "", b.page_count or "",
            loc, b.added_at.strftime("%Y-%m-%d") if b.added_at else "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=bibliodech.csv"},
    )


@app.get("/api/music/export/csv")
def export_discs_csv(request: Request, db: Session = Depends(get_db)):
    from app.models import Disc
    get_current_user(request, db)
    discs = db.query(Disc).order_by(Disc.artist, Disc.title).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Code-barres", "Titre", "Artiste", "Label", "N° catalogue", "Format", "Année",
                     "Genre", "Pistes", "Pays", "Langue", "Localisation", "Ajouté le"])
    for d in discs:
        if d.room:
            loc = f"{d.room.site.name} / {d.room.name}" if d.room.site else d.room.name
        else:
            loc = ""
        writer.writerow([
            d.barcode or "", d.title or "", d.artist or "", d.label or "",
            d.catalog_number or "", d.format or "", d.year or "",
            d.genre or "", d.track_count or "", d.country or "", d.language or "",
            loc, d.added_at.strftime("%Y-%m-%d") if d.added_at else "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=bibliodech-musique.csv"},
    )


@app.get("/api/search")
def search_all(q: str, request: Request, db: Session = Depends(get_db)):
    from app.models import Disc
    get_current_user(request, db)
    if not q or len(q.strip()) < 2:
        return {"books": [], "discs": []}
    term = f"%{q.strip()}%"

    books_q = db.query(Book).filter(
        Book.title.ilike(term) |
        Book.authors.ilike(term) |
        Book.isbn.ilike(term)
    ).limit(8).all()

    discs_q = db.query(Disc).filter(
        Disc.title.ilike(term) |
        Disc.artist.ilike(term) |
        Disc.barcode.ilike(term)
    ).limit(8).all()

    def book_dict(b):
        authors = json.loads(b.authors) if b.authors else []
        return {"id": b.id, "title": b.title, "subtitle": b.subtitle,
                "authors": authors, "cover_url": b.cover_url, "type": "book"}

    def disc_dict(d):
        return {"id": d.id, "title": d.title, "artist": d.artist,
                "cover_url": d.cover_url, "format": d.format, "type": "disc"}

    return {"books": [book_dict(b) for b in books_q], "discs": [disc_dict(d) for d in discs_q]}


@app.get("/api/stats")
def get_stats(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import func, extract
    from app.models import Room, Loan, Borrower
    get_current_user(request, db)

    # ── Chiffres clés ─────────────────────────────────────────────────────────
    total_books   = db.query(func.count(Book.id)).scalar()
    active_loans  = db.query(func.count(Loan.id)).filter(Loan.return_date.is_(None)).scalar()

    # Auteurs uniques (dédoublonnage via JSON)
    all_authors_raw = db.query(Book.authors).filter(Book.authors.isnot(None)).all()
    unique_authors = set()
    for (a,) in all_authors_raw:
        try:
            for name in json.loads(a):
                if name.strip():
                    unique_authors.add(name.strip())
        except Exception:
            pass

    # ── Livres par pièce ──────────────────────────────────────────────────────
    rooms = db.query(Room).all()
    by_location = []
    for r in rooms:
        count = db.query(func.count(Book.id)).filter(Book.room_id == r.id).scalar()
        if count:
            label = f"{r.site.name} › {r.name}" if r.site else r.name
            by_location.append({"label": label, "count": count})
    no_loc = db.query(func.count(Book.id)).filter(Book.room_id.is_(None)).scalar()
    if no_loc:
        by_location.append({"label": "Sans localisation", "count": no_loc})
    by_location.sort(key=lambda x: x["count"], reverse=True)

    # ── Top auteurs ───────────────────────────────────────────────────────────
    author_counts: dict[str, int] = {}
    for (a,) in all_authors_raw:
        try:
            for name in json.loads(a):
                name = name.strip()
                if name:
                    author_counts[name] = author_counts.get(name, 0) + 1
        except Exception:
            pass
    top_authors = sorted(author_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    top_authors = [{"name": n, "count": c} for n, c in top_authors]

    # ── Ajouts par jour (30 derniers jours) ──────────────────────────────────
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    months = []
    for i in range(29, -1, -1):
        day_dt = (now - timedelta(days=i)).date()
        label = day_dt.strftime("%-d %b")
        count = db.query(func.count(Book.id)).filter(
            func.strftime("%Y-%m-%d", Book.added_at) == day_dt.strftime("%Y-%m-%d")
        ).scalar()
        months.append({"label": label, "count": count})

    # ── Par source d'enrichissement ───────────────────────────────────────────
    source_rows = db.query(Book.enrichment_source, func.count(Book.id))\
        .group_by(Book.enrichment_source).order_by(func.count(Book.id).desc()).all()
    from app.routers.books import SOURCE_LABELS
    by_source = [
        {"id": src or "none", "label": SOURCE_LABELS.get(src, src or "Manuel / Import"), "count": cnt}
        for src, cnt in source_rows
    ]

    # ── Langues ───────────────────────────────────────────────────────────────
    lang_rows = db.query(Book.language, func.count(Book.id))\
        .filter(Book.language.isnot(None), Book.language != "")\
        .group_by(Book.language).order_by(func.count(Book.id).desc()).all()
    LANG_LABELS = {
        "fr": "Français", "fre": "Français", "fra": "Français",
        "en": "Anglais",  "eng": "Anglais",
        "de": "Allemand", "ger": "Allemand", "deu": "Allemand",
        "es": "Espagnol", "spa": "Espagnol",
        "it": "Italien",  "ita": "Italien",
        "pt": "Portugais","por": "Portugais",
        "nl": "Néerlandais","nld": "Néerlandais","dut": "Néerlandais",
        "ja": "Japonais", "jpn": "Japonais",
        "zh": "Chinois",  "chi": "Chinois",  "zho": "Chinois",
        "ar": "Arabe",    "ara": "Arabe",
        "ru": "Russe",    "rus": "Russe",
    }
    languages = [{"code": lang, "label": LANG_LABELS.get(lang, lang), "count": cnt}
                 for lang, cnt in lang_rows]

    # ── Prêts ─────────────────────────────────────────────────────────────────
    total_loans    = db.query(func.count(Loan.id)).scalar()
    returned_loans = db.query(func.count(Loan.id)).filter(Loan.return_date.isnot(None)).scalar()

    # Top emprunteurs
    borrow_counts = {}
    for loan in db.query(Loan).all():
        if loan.borrower:
            name = loan.borrower.name
        elif loan.user:
            name = loan.user.username
        else:
            continue
        borrow_counts[name] = borrow_counts.get(name, 0) + 1
    top_borrowers = sorted(borrow_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    top_borrowers = [{"name": n, "count": c} for n, c in top_borrowers]

    # Livres les plus prêtés
    book_loan_counts = db.query(Loan.book_id, func.count(Loan.id))\
        .group_by(Loan.book_id).order_by(func.count(Loan.id).desc()).limit(5).all()
    top_loaned = []
    for book_id, cnt in book_loan_counts:
        b = db.query(Book).filter(Book.id == book_id).first()
        if b:
            top_loaned.append({"title": b.title, "count": cnt})

    # ── Séries ────────────────────────────────────────────────────────────────
    all_series = db.query(Series).all()
    total_series = len(all_series)
    books_in_series = db.query(func.count(Book.id)).filter(Book.series_id.isnot(None)).scalar()
    books_no_series = total_books - books_in_series
    empty_series = sum(1 for s in all_series if len(s.books) == 0)
    top_series = sorted(
        [{"name": s.name, "count": len(s.books)} for s in all_series if s.books],
        key=lambda x: x["count"], reverse=True
    )[:10]

    # ── Stats musique ──────────────────────────────────────────────────────────
    from app.models import Disc
    total_discs = db.query(func.count(Disc.id)).scalar()
    top_artists_rows = db.query(Disc.artist, func.count(Disc.id))\
        .filter(Disc.artist.isnot(None))\
        .group_by(Disc.artist).order_by(func.count(Disc.id).desc()).limit(10).all()
    top_artists = [{"name": a, "count": c} for a, c in top_artists_rows]
    format_rows = db.query(Disc.format, func.count(Disc.id))\
        .filter(Disc.format.isnot(None))\
        .group_by(Disc.format).order_by(func.count(Disc.id).desc()).all()
    by_format = [{"label": f, "count": c} for f, c in format_rows]

    return {
        "totals": {
            "books": total_books,
            "authors": len(unique_authors),
            "active_loans": active_loans,
            "series": total_series,
        },
        "by_location": by_location,
        "by_source": by_source,
        "top_authors": top_authors,
        "by_month": months,
        "languages": languages,
        "loans": {
            "total": total_loans,
            "returned": returned_loans,
            "active": active_loans,
            "top_borrowers": top_borrowers,
            "top_loaned": top_loaned,
        },
        "series": {
            "total": total_series,
            "books_in_series": books_in_series,
            "books_no_series": books_no_series,
            "empty_series": empty_series,
            "top_series": top_series,
        },
        "music": {
            "total": total_discs,
            "top_artists": top_artists,
            "by_format": by_format,
        },
    }


@app.get("/api/books/import/template")
def import_template(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ISBN", "Localisation"])
    writer.writerow(["9782070360024", "Maison > Salon"])
    writer.writerow(["9782253004226", ""])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=bibliodech_modele.csv"},
    )


@app.post("/api/books/import/csv")
async def import_books_csv(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    from fastapi import HTTPException
    from app.models import Room
    from app.auth import require_contributor
    from app.routers.scan import _enrich_book
    from datetime import datetime

    user = get_current_user(request, db)
    require_contributor(user)

    form = await request.form()
    upload = form.get("file")
    if not upload:
        raise HTTPException(status_code=400, detail="Fichier manquant")

    raw = await upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    def norm(row: dict) -> dict:
        return {k.strip().lower(): v.strip() for k, v in row.items()}

    # Cache rooms
    all_rooms = db.query(Room).all()
    room_map: dict[str, int] = {}
    for r in all_rooms:
        site_name = r.site.name if r.site else None
        if site_name:
            room_map[f"{site_name} › {r.name}".lower()] = r.id
            room_map[f"{site_name} > {r.name}".lower()] = r.id
        room_map[r.name.lower()] = r.id

    # Cache séries (pour import complet)
    from app.models import Series
    series_map: dict[str, int] = {s.name.lower(): s.id for s in db.query(Series).all()}

    queued, created, skipped, errors = 0, 0, 0, []

    for i, raw_row in enumerate(reader, start=2):
        row = norm(raw_row)
        isbn = row.get("isbn", "").strip().replace("-", "").replace(" ", "")
        title = row.get("titre", "").strip()

        if not isbn and not title:
            skipped += 1
            continue

        # Doublon ISBN
        if isbn and db.query(Book).filter(Book.isbn == isbn).first():
            skipped += 1
            continue

        loc_label = row.get("localisation", "").strip()
        room_id = room_map.get(loc_label.lower()) if loc_label else None

        # ── Format complet (export Bibliodech) : colonne Titre présente ──────
        if title:
            authors_raw = row.get("auteurs", "")
            authors = [a.strip() for a in authors_raw.split(",") if a.strip()]

            series_id = None
            sname = row.get("série", row.get("serie", "")).strip()
            if sname:
                key = sname.lower()
                if key not in series_map:
                    s = Series(name=sname, source="import")
                    db.add(s)
                    db.flush()
                    series_map[key] = s.id
                series_id = series_map[key]

            spos = row.get("position", "")
            try:
                series_position = float(spos) if spos else None
            except ValueError:
                series_position = None

            pages = row.get("pages", "")
            try:
                page_count = int(pages) if pages else None
            except ValueError:
                page_count = None

            try:
                book = Book(
                    isbn=isbn or None,
                    title=title,
                    subtitle=row.get("sous-titre") or None,
                    authors=json.dumps(authors),
                    publisher=row.get("éditeur", row.get("editeur")) or None,
                    publish_date=row.get("date") or None,
                    language=row.get("langue") or None,
                    page_count=page_count,
                    source="import",
                    series_id=series_id,
                    series_position=series_position,
                    room_id=room_id,
                    added_at=datetime.utcnow(),
                    enrichment_status="ok",
                )
                db.add(book)
                created += 1
            except Exception as e:
                errors.append({"ligne": i, "isbn": isbn, "erreur": str(e)})

        # ── Format minimal : ISBN seulement → enrichissement en arrière-plan ──
        else:
            try:
                book = Book(
                    isbn=isbn,
                    title=isbn,
                    authors=json.dumps([]),
                    source="pending",
                    room_id=room_id,
                    added_at=datetime.utcnow(),
                    enrichment_status="pending",
                )
                db.add(book)
                db.flush()
                background_tasks.add_task(_enrich_book, book.id, isbn)
                queued += 1
            except Exception as e:
                errors.append({"ligne": i, "isbn": isbn, "erreur": str(e)})

    db.commit()
    return {"queued": queued, "created": created, "skipped": skipped, "errors": errors}


@app.get("/api/debug/isbn/{isbn}")
async def debug_isbn_endpoint(isbn: str, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)  # auth required
    return await debug_isbn(isbn)


@app.get("/locations", response_class=HTMLResponse)
def locations_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    if redir := _require_pw_changed(user): return redir
    return templates.TemplateResponse("locations.html", {"request": request, "user": user, "active": "locations", "build_version": BUILD_VERSION})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
        if user.role != "admin":
            return RedirectResponse(url="/library", status_code=302)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "active": "settings", "build_version": BUILD_VERSION})


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
        if user.role != "admin":
            return RedirectResponse(url="/library", status_code=302)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("users.html", {"request": request, "user": user, "active": "users", "build_version": BUILD_VERSION})


@app.get("/loans", response_class=HTMLResponse)
def loans_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    if redir := _require_pw_changed(user): return redir
    return templates.TemplateResponse("loans.html", {"request": request, "user": user, "active": "loans", "build_version": BUILD_VERSION})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    if redir := _require_pw_changed(user): return redir
    return templates.TemplateResponse("stats.html", {"request": request, "user": user, "active": "stats", "build_version": BUILD_VERSION})


@app.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    return templates.TemplateResponse("applogs.html", {"request": request, "user": user, "active": "logs", "build_version": BUILD_VERSION})


@app.get("/series", response_class=HTMLResponse)
def series_browse_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("series_browse.html", {
        "request": request, "user": user, "active": "series-browse", "build_version": BUILD_VERSION,
    })


@app.get("/missing-volumes", response_class=HTMLResponse)
def missing_volumes_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("missing_volumes.html", {
        "request": request, "user": user, "active": "missing-volumes", "build_version": BUILD_VERSION,
    })


@app.get("/series/proposals", response_class=HTMLResponse)
def series_proposals_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
        if user.role != "admin":
            return RedirectResponse(url="/library", status_code=302)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    from app.models import SeriesProposal
    pending = db.query(SeriesProposal).filter(SeriesProposal.status == "pending").count()
    series_list = db.query(Series).order_by(Series.name).all()
    return templates.TemplateResponse("series_proposals.html", {
        "request": request,
        "user": user,
        "active": "series",
        "build_version": BUILD_VERSION,
        "pending_count": pending,
        "series_list": [{"id": s.id, "name": s.name} for s in series_list],
    })


# ── Documentation ─────────────────────────────────────────────────────────────
import pathlib
from fastapi.responses import PlainTextResponse

_DOCS_ROOT = pathlib.Path(__file__).parent.parent

_DOC_FILES = [
    ("FONCTIONNALITES",     "docs/FONCTIONNALITES.md",     "Fonctionnalités"),
    ("STACK",               "docs/STACK.md",               "Stack technique"),
    ("SERIE_ASSIGNATION",   "docs/SERIE_ASSIGNATION.md",   "Assignation des séries"),
    ("SCRIPTS",             "docs/SCRIPTS.md",             "Scripts Python"),
]

@app.get("/aide", response_class=HTMLResponse)
def docs_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except Exception:
        return RedirectResponse(url="/login", status_code=302)
    files = [{"slug": slug, "label": label} for slug, _, label in _DOC_FILES]
    return templates.TemplateResponse("docs.html", {
        "request": request, "user": user, "active": "docs",
        "build_version": BUILD_VERSION, "doc_files": files,
    })

@app.get("/api/aide/{slug}", response_class=PlainTextResponse)
def docs_content(slug: str, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    from fastapi import HTTPException
    entry = next((e for e in _DOC_FILES if e[0] == slug), None)
    if not entry:
        raise HTTPException(404)
    path = _DOCS_ROOT / entry[1]
    if not path.exists():
        raise HTTPException(404, "Fichier introuvable")
    return path.read_text(encoding="utf-8")
