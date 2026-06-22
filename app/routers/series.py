import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import Book, Series, SeriesProposal

router = APIRouter()

_ARTICLES = {"le", "la", "les", "l", "un", "une", "des", "du", "the", "a", "an"}


# ── Normalisation ─────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[''\"«»&]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_pub(p: str | None) -> str:
    if not p:
        return ""
    return _norm(p.split("(")[0])


def _norm_authors(a: str | None) -> frozenset:
    try:
        return frozenset(_norm(x) for x in json.loads(a or "[]"))
    except Exception:
        return frozenset()


def _first_word(title: str) -> str | None:
    """Premier mot significatif du titre (ignore les articles)."""
    t = _norm(title)
    for word in t.split():
        w = re.sub(r"[^a-z0-9]", "", word)
        if w and w not in _ARTICLES and len(w) >= 3:
            return w
    return None


# ── Requête Google Books pour le nom de série ─────────────────────────────────

async def _google_series_name(isbn: str, api_key: str = "") -> str | None:
    if not isbn:
        return None
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
    if api_key:
        url += f"&key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            items = r.json().get("items", [])
            if not items:
                return None
            si = items[0].get("volumeInfo", {}).get("seriesInfo", {})
            return si.get("shortSeriesBookTitle") or si.get("bookSeries", [{}])[0].get("title")
    except Exception:
        return None


# ── Algorithme de détection ───────────────────────────────────────────────────

async def _detect(db: Session) -> dict:
    import app.settings as cfg
    gb_key = cfg.get(db, "googlebooks_api_key") or ""

    books = (
        db.query(Book)
        .filter(Book.enrichment_status == "ok", Book.series_id.is_(None))
        .all()
    )

    # Index : livres déjà en série
    series_books = db.query(Book).filter(Book.series_id.isnot(None)).all()

    # (auteur_set, pub) → set(series_id)
    author_pub_series: dict[tuple, set] = defaultdict(set)
    for b in series_books:
        pub = _norm_pub(b.publisher)
        for a in _norm_authors(b.authors):
            author_pub_series[(a, pub)].add(b.series_id)

    # (first_word, pub) → set(series_id)
    prefix_pub_series: dict[tuple, set] = defaultdict(set)
    for b in series_books:
        fw = _first_word(b.title)
        pub = _norm_pub(b.publisher)
        if fw:
            prefix_pub_series[(fw, pub)].add(b.series_id)

    # Grouper les livres orphelins par (first_word, pub) et (authors, pub)
    prefix_groups: dict[tuple, list] = defaultdict(list)
    author_groups: dict[tuple, list] = defaultdict(list)

    for b in books:
        fw = _first_word(b.title)
        pub = _norm_pub(b.publisher)
        if fw:
            prefix_groups[(fw, pub)].append(b)
        au = _norm_authors(b.authors)
        if au:
            author_groups[(au, pub)].append(b)

    auto_assigned = 0
    proposals_created = 0
    already_proposed_book_sets: list[frozenset] = [
        frozenset(json.loads(p.book_ids))
        for p in db.query(SeriesProposal).filter(SeriesProposal.status == "pending").all()
    ]

    def _already_proposed(book_ids: list[int]) -> bool:
        s = frozenset(book_ids)
        return any(s == existing for existing in already_proposed_book_sets)

    # ── Signal 1 : préfixe + éditeur ────────────────────────────────────────
    for (fw, pub), group in prefix_groups.items():
        if len(group) < 1:
            continue
        known_sids = prefix_pub_series.get((fw, pub), set())

        if len(known_sids) == 1:
            # Haute confiance : relier directement
            sid = next(iter(known_sids))
            for b in group:
                b.series_id = sid
                auto_assigned += 1
            db.commit()

        elif len(known_sids) == 0 and len(group) >= 2:
            # Nouveau groupe potentiel → proposition
            ids = [b.id for b in group]
            if _already_proposed(ids):
                continue
            # Chercher le nom via Google Books sur le premier livre avec ISBN
            name = None
            for b in group:
                if b.isbn:
                    name = await _google_series_name(b.isbn, gb_key)
                    if name:
                        break
            p = SeriesProposal(
                book_ids=json.dumps(ids),
                proposed_name=name,
                signal="prefix",
                status="pending",
            )
            db.add(p)
            proposals_created += 1
            already_proposed_book_sets.append(frozenset(ids))

    db.commit()

    # ── Signal 2 : auteur + éditeur ─────────────────────────────────────────
    # Livres encore sans série après le signal 1
    books_still_orphan = (
        db.query(Book)
        .filter(Book.enrichment_status == "ok", Book.series_id.is_(None))
        .all()
    )

    author_groups2: dict[tuple, list] = defaultdict(list)
    for b in books_still_orphan:
        pub = _norm_pub(b.publisher)
        au = _norm_authors(b.authors)
        if au:
            author_groups2[(au, pub)].append(b)

    for (au, pub), group in author_groups2.items():
        if len(group) < 1:
            continue
        # Chercher les séries connues pour cet auteur+éditeur (après signal 1)
        known_sids: set = set()
        for a in au:
            known_sids |= author_pub_series.get((a, pub), set())

        if len(known_sids) == 1:
            sid = next(iter(known_sids))
            for b in group:
                b.series_id = sid
                auto_assigned += 1
            db.commit()

        elif len(known_sids) > 1:
            # Ambigu → proposition à valider
            ids = [b.id for b in group]
            if _already_proposed(ids):
                continue
            name = None
            for b in group:
                if b.isbn:
                    name = await _google_series_name(b.isbn, gb_key)
                    if name:
                        break
            p = SeriesProposal(
                book_ids=json.dumps(ids),
                proposed_name=name,
                signal="author",
                status="pending",
            )
            db.add(p)
            proposals_created += 1
            already_proposed_book_sets.append(frozenset(ids))

        elif len(known_sids) == 0 and len(group) >= 2:
            # Nouveau groupe sans série connue → proposition
            ids = [b.id for b in group]
            if _already_proposed(ids):
                continue
            name = None
            for b in group:
                if b.isbn:
                    name = await _google_series_name(b.isbn, gb_key)
                    if name:
                        break
            p = SeriesProposal(
                book_ids=json.dumps(ids),
                proposed_name=name,
                signal="author",
                status="pending",
            )
            db.add(p)
            proposals_created += 1
            already_proposed_book_sets.append(frozenset(ids))

    db.commit()

    return {"auto_assigned": auto_assigned, "proposals": proposals_created}


# ── API ───────────────────────────────────────────────────────────────────────

@router.post("/api/series/detect")
async def detect_series(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    result = await _detect(db)
    return result


@router.get("/api/series/proposals")
def list_proposals(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    proposals = (
        db.query(SeriesProposal)
        .filter(SeriesProposal.status == "pending")
        .order_by(SeriesProposal.detected_at.desc())
        .all()
    )
    out = []
    for p in proposals:
        ids = json.loads(p.book_ids)
        books = db.query(Book).filter(Book.id.in_(ids)).all()
        out.append({
            "id": p.id,
            "signal": p.signal,
            "proposed_name": p.proposed_name,
            "existing_series_id": p.existing_series_id,
            "existing_series_name": p.existing_series.name if p.existing_series else None,
            "detected_at": p.detected_at.isoformat() if p.detected_at else None,
            "books": [
                {
                    "id": b.id,
                    "title": b.title,
                    "authors": json.loads(b.authors or "[]"),
                    "cover_url": b.cover_url,
                    "series_id": b.series_id,
                }
                for b in books
            ],
        })
    return out


@router.post("/api/series/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    body: { name: str, existing_series_id: int | null }
    Si existing_series_id fourni → relier à cette série.
    Sinon → créer une nouvelle série avec `name`.
    """
    user = get_current_user(request, db)
    require_admin(user)

    from fastapi import HTTPException
    p = db.query(SeriesProposal).filter(SeriesProposal.id == proposal_id).first()
    if not p:
        raise HTTPException(404, "Proposition introuvable")

    existing_sid = body.get("existing_series_id")
    name = (body.get("name") or "").strip()

    if existing_sid:
        series = db.query(Series).filter(Series.id == existing_sid).first()
        if not series:
            raise HTTPException(400, "Série introuvable")
    elif name:
        series = db.query(Series).filter(Series.name == name).first()
        if not series:
            series = Series(name=name, source="detected")
            db.add(series)
            db.flush()
    else:
        raise HTTPException(400, "Nom de série requis")

    ids = json.loads(p.book_ids)
    books = db.query(Book).filter(Book.id.in_(ids)).all()
    for b in books:
        b.series_id = series.id

    p.status = "accepted"
    db.commit()
    return {"ok": True, "series_id": series.id, "series_name": series.name, "linked": len(books)}


@router.post("/api/series/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    from fastapi import HTTPException
    p = db.query(SeriesProposal).filter(SeriesProposal.id == proposal_id).first()
    if not p:
        raise HTTPException(404, "Proposition introuvable")
    p.status = "rejected"
    db.commit()
    return {"ok": True}


@router.get("/api/series")
def list_series(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series = db.query(Series).order_by(Series.name).all()
    return [
        {"id": s.id, "name": s.name, "book_count": len(s.books)}
        for s in series
    ]
