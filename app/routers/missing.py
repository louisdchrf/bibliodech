"""
Tomes manquants dans les séries.
"""
import asyncio
import json
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_contributor
from app.database import get_db
from app.models import Book, Series, SeriesMissingVolume
from app.series_search import search_complete_volume_list

router = APIRouter()

import re as _re


def _find_library_matches(db: Session) -> list[dict]:
    """
    Pour chaque série, cherche dans toute la bibliothèque les livres
    dont le titre correspond à un tome manquant (pas encore assigné à la série).
    Retourne une liste de correspondances {series, book, suggested_position}.
    """
    series_list = db.query(Series).order_by(Series.name).all()
    matches = []

    for series in series_list:
        owned = db.query(Book).filter(Book.series_id == series.id).all()
        owned_ids = {b.id for b in owned}
        owned_positions = {b.series_position for b in owned if b.series_position is not None}

        # Mots significatifs du nom de série (≥ 3 chars)
        words = [w for w in series.name.lower().split() if len(w) >= 3]
        if not words:
            continue

        # Chercher dans tous les livres qui ne sont pas dans cette série
        candidates = db.query(Book).filter(Book.id.notin_(owned_ids)).all()
        for book in candidates:
            title_lower = (book.title or "").lower()
            # Le titre doit contenir les 2 premiers mots significatifs de la série
            if not all(w in title_lower for w in words[:2]):
                continue

            # Extraire le numéro de tome du titre du livre
            m = _re.search(r'(?:tome|vol\.?|t\.)\s*(\d+)', title_lower)
            if not m:
                # Essayer un numéro seul en fin de titre : "Lady S 4" ou "Lady S. - 4"
                m = _re.search(r'[-–\s](\d{1,2})\s*$', title_lower)
            if not m:
                continue

            pos = float(m.group(1))
            if pos in owned_positions:
                continue  # Déjà dans la série à cette position

            matches.append({
                "series_id": series.id,
                "series_name": series.name,
                "book_id": book.id,
                "book_title": book.title,
                "book_authors": json.loads(book.authors) if book.authors else [],
                "book_cover": book.cover_url,
                "current_series_id": book.series_id,
                "current_series_name": book.series.name if book.series else None,
                "suggested_position": pos,
            })

    return matches



def _series_missing_data(series: Series, db: Session) -> dict:
    """Calcule les données de tomes manquants pour une série."""
    books = (
        db.query(Book)
        .filter(Book.series_id == series.id)
        .order_by(Book.series_position.nullslast(), Book.title)
        .all()
    )

    owned_positions = sorted({b.series_position for b in books if b.series_position is not None})

    # Gaps dans la séquence connue
    gaps: list[float] = []
    if len(owned_positions) >= 2:
        for i in range(int(owned_positions[0]), int(owned_positions[-1])):
            pos = float(i)
            if pos not in owned_positions:
                gaps.append(pos)

    # Candidats : tous les livres dont le titre contient le nom de la série
    # (même s'ils sont assignés à une autre série)
    series_words = series.name.lower().split()
    candidates = []
    if len(series_words) >= 2:
        all_books = db.query(Book).filter(Book.id.notin_([b.id for b in books])).all()
        for b in all_books:
            title_lower = (b.title or "").lower()
            if all(w in title_lower for w in series_words[:2]):
                candidates.append({
                    "id": b.id,
                    "title": b.title,
                    "series_position": b.series_position,
                    "current_series_id": b.series_id,
                    "current_series_name": b.series.name if b.series else None,
                    "cover_url": b.cover_url,
                    "authors": json.loads(b.authors) if b.authors else [],
                })

    # Tomes connus manquants (stockés en DB via recherche web)
    stored_missing = (
        db.query(SeriesMissingVolume)
        .filter(SeriesMissingVolume.series_id == series.id)
        .order_by(SeriesMissingVolume.position.nullslast())
        .all()
    )

    # Filtrer les manquants stockés qui ont été trouvés depuis
    missing_to_buy = [
        {
            "id": m.id,
            "position": m.position,
            "title": m.title,
            "detected_at": m.detected_at.isoformat() if m.detected_at else None,
        }
        for m in stored_missing
        if not any(b.series_position == m.position for b in books)
    ]

    return {
        "series": {
            "id": series.id,
            "name": series.name,
            "book_count": len(books),
        },
        "owned": [
            {
                "id": b.id,
                "title": b.title,
                "position": b.series_position,
                "cover_url": b.cover_url,
            }
            for b in books
        ],
        "gaps": gaps,
        "candidates": candidates,
        "missing_to_buy": missing_to_buy,
        "has_issues": bool(gaps or candidates or missing_to_buy),
    }


@router.get("/api/missing/library-matches")
def get_library_matches(request: Request, db: Session = Depends(get_db)):
    """Retourne tous les livres de la bibliothèque qui correspondent à des tomes manquants."""
    get_current_user(request, db)
    return _find_library_matches(db)


@router.post("/api/missing/auto-assign")
def auto_assign_matches(request: Request, db: Session = Depends(get_db)):
    """Assigne automatiquement les correspondances évidentes (1 seul candidat par position)."""
    user = get_current_user(request, db)
    require_contributor(user)
    matches = _find_library_matches(db)

    # Grouper par (series_id, position) — assigner uniquement si 1 seul candidat
    from collections import defaultdict
    by_slot: dict[tuple, list] = defaultdict(list)
    for m in matches:
        by_slot[(m["series_id"], m["suggested_position"])].append(m)

    assigned = 0
    for (series_id, pos), candidates in by_slot.items():
        if len(candidates) == 1:
            book = db.query(Book).filter(Book.id == candidates[0]["book_id"]).first()
            if book:
                book.series_id = series_id
                book.series_position = pos
                assigned += 1

    db.commit()
    return {"assigned": assigned, "ambiguous": sum(1 for c in by_slot.values() if len(c) > 1)}


@router.get("/api/missing")
def get_all_missing(request: Request, db: Session = Depends(get_db)):
    """Retourne toutes les séries avec des tomes manquants ou des candidats."""
    get_current_user(request, db)
    series_list = db.query(Series).order_by(Series.name).all()
    result = []
    for s in series_list:
        data = _series_missing_data(s, db)
        if data["has_issues"]:
            result.append(data)
    return result


@router.get("/api/missing/{series_id}")
def get_series_missing(series_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404)
    return _series_missing_data(series, db)


def _store_missing_volumes(series_id: int, volumes: list[dict], owned_positions: set, db) -> list:
    """Persiste les volumes trouvés en ligne qui ne sont pas déjà possédés."""
    db.query(SeriesMissingVolume).filter(SeriesMissingVolume.series_id == series_id).delete()
    added = []
    for v in volumes:
        pos = v.get("position")
        if pos is not None and pos not in owned_positions:
            db.add(SeriesMissingVolume(
                series_id=series_id,
                position=pos,
                title=v.get("title"),
                detected_at=datetime.utcnow(),
            ))
            added.append(pos)
    return added


@router.post("/api/missing/search-all-web")
async def search_all_missing_web(request: Request, db: Session = Depends(get_db)):
    """Lance la recherche Babelio/DDG de tomes manquants pour toutes les séries."""
    user = get_current_user(request, db)
    require_contributor(user)
    series_list = db.query(Series).order_by(Series.name).all()
    total_added = 0
    series_checked = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for i, series in enumerate(series_list):
            if i > 0:
                await asyncio.sleep(2.0)
            owned = db.query(Book).filter(Book.series_id == series.id).all()
            owned_positions = {b.series_position for b in owned if b.series_position is not None}
            if not owned_positions:
                continue
            volumes = await search_complete_volume_list(series.name, client)
            added = _store_missing_volumes(series.id, volumes, owned_positions, db)
            total_added += len(added)
            series_checked += 1
    db.commit()
    return {"series_checked": series_checked, "missing_added": total_added}


@router.post("/api/missing/{series_id}/search-web")
async def search_missing_web(series_id: int, request: Request, db: Session = Depends(get_db)):
    """Cherche via Babelio/DDG la liste complète des tomes de la série."""
    user = get_current_user(request, db)
    require_contributor(user)
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404)

    async with httpx.AsyncClient(timeout=20) as client:
        volumes = await search_complete_volume_list(series.name, client)

    owned = db.query(Book).filter(Book.series_id == series_id).all()
    owned_positions = {b.series_position for b in owned if b.series_position is not None}

    added = _store_missing_volumes(series_id, volumes, owned_positions, db)
    db.commit()

    return {
        "found_volumes": volumes,
        "owned_positions": sorted(owned_positions),
        "missing_added": added,
    }


@router.post("/api/missing/{missing_id}/resolve")
def resolve_missing(missing_id: int, request: Request, db: Session = Depends(get_db)):
    """Marque un tome manquant comme résolu (acheté/trouvé)."""
    user = get_current_user(request, db)
    require_contributor(user)
    mv = db.query(SeriesMissingVolume).filter(SeriesMissingVolume.id == missing_id).first()
    if not mv:
        raise HTTPException(status_code=404)
    db.delete(mv)
    db.commit()
    return {"ok": True}


@router.post("/api/books/{book_id}/assign-series")
def assign_series(book_id: int, body: dict, request: Request, db: Session = Depends(get_db)):
    """Assigne un livre à une série (et optionnellement une position)."""
    user = get_current_user(request, db)
    require_contributor(user)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404)
    book.series_id = body.get("series_id")
    if "position" in body:
        book.series_position = body["position"]
    db.commit()
    return {"ok": True}
